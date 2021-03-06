"""
Fetches connection data (IP addresses and ports) associated with a given
process. This sort of data can be retrieved via a variety of common *nix
utilities:
- netstat   netstat -np | grep "ESTABLISHED <pid>/<process>"
- sockstat  sockstat | egrep "<process>\s*<pid>.*ESTABLISHED"
- lsof      lsof -nPi | egrep "^<process>\s*<pid>.*((UDP.*)|(\(ESTABLISHED\)))"
- ss        ss -nptu | grep "ESTAB.*\"<process>\",<pid>"

all queries dump its stderr (directing it to /dev/null). Results include UDP
and established TCP connections.

FreeBSD lacks support for the needed netstat flags and has a completely
different program for 'ss'. However, lsof works and there's a couple other
options that perform even better (thanks to Fabian Keil and Hans Schnehl):
- sockstat    sockstat -4c | grep '<process> *<pid>'
- procstat    procstat -f <pid> | grep TCP | grep -v 0.0.0.0:0
"""

import os
import sys
import time
import threading

from util import log, sysTools

# enums for connection resolution utilities
CMD_NETSTAT, CMD_SOCKSTAT, CMD_LSOF, CMD_SS, CMD_BSD_SOCKSTAT, CMD_BSD_PROCSTAT = range(1, 7)
CMD_STR = {CMD_NETSTAT: "netstat",
           CMD_SS: "ss",
           CMD_LSOF: "lsof",
           CMD_SOCKSTAT: "sockstat",
           CMD_BSD_SOCKSTAT: "sockstat (bsd)",
           CMD_BSD_PROCSTAT: "procstat (bsd)"}

# If true this provides new instantiations for resolvers if the old one has
# been stopped. This can make it difficult ensure all threads are terminated
# when accessed concurrently.
RECREATE_HALTED_RESOLVERS = False

# formatted strings for the commands to be executed with the various resolvers
# options are:
# n = prevents dns lookups, p = include process
# output:
# tcp  0  0  127.0.0.1:9051  127.0.0.1:53308  ESTABLISHED 9912/tor
# *note: bsd uses a different variant ('-t' => '-p tcp', but worse an
#   equivilant -p doesn't exist so this can't function)
RUN_NETSTAT = "netstat -np | grep \"ESTABLISHED %s/%s\""

# n = numeric ports, p = include process, t = tcp sockets, u = udp sockets
# output:
# ESTAB  0  0  127.0.0.1:9051  127.0.0.1:53308  users:(("tor",9912,20))
# *note: under freebsd this command belongs to a spreadsheet program
RUN_SS = "ss -nptu | grep \"ESTAB.*\\\"%s\\\",%s\""

# n = prevent dns lookups, P = show port numbers (not names), i = ip only
# output:
# tor  3873  atagar  45u  IPv4  40994  0t0  TCP 10.243.55.20:45724->194.154.227.109:9001 (ESTABLISHED)
# 
# oddly, using the -p flag via:
# lsof      lsof -nPi -p <pid> | grep "^<process>.*(ESTABLISHED)"
# is much slower (11-28% in tests I ran)
RUN_LSOF = "lsof -nPi | egrep \"^%s\\s*%s.*((UDP.*)|(\\(ESTABLISHED\\)))\""

# output:
# atagar  tor  3475  tcp4  127.0.0.1:9051  127.0.0.1:38942  ESTABLISHED
# *note: this isn't available by default under ubuntu
RUN_SOCKSTAT = "sockstat | egrep \"%s\s*%s.*ESTABLISHED\""

RUN_BSD_SOCKSTAT = "sockstat -4c | grep '%s *%s'"
RUN_BSD_PROCSTAT = "procstat -f %s | grep TCP | grep -v 0.0.0.0:0"

RESOLVERS = []                      # connection resolvers available via the singleton constructor
RESOLVER_FAILURE_TOLERANCE = 3      # number of subsequent failures before moving on to another resolver
RESOLVER_SERIAL_FAILURE_MSG = "Querying connections with %s failed, trying %s"
RESOLVER_FINAL_FAILURE_MSG = "All connection resolvers failed"
CONFIG = {"queries.connections.minRate": 5,
          "log.connResolverOptions": log.INFO,
          "log.connLookupFailed": log.INFO,
          "log.connLookupFailover": log.NOTICE,
          "log.connLookupAbandon": log.WARN,
          "log.connLookupRateGrowing": None}

def loadConfig(config):
  config.update(CONFIG)

def getResolverCommand(resolutionCmd, processName, processPid = ""):
  """
  Provides the command that would be processed for the given resolver type.
  This raises a ValueError if either the resolutionCmd isn't recognized or a
  pid was requited but not provided.
  
  Arguments:
    resolutionCmd - command to use in resolving the address
    processName   - name of the process for which connections are fetched
    processPid    - process ID (this helps improve accuracy)
  """
  
  if not processPid:
    # the pid is required for procstat resolution
    if resolutionCmd == CMD_BSD_PROCSTAT:
      raise ValueError("procstat resolution requires a pid")
    
    # if the pid was undefined then match any in that field
    processPid = "[0-9]*"
  
  if resolutionCmd == CMD_NETSTAT: return RUN_NETSTAT % (processPid, processName)
  elif resolutionCmd == CMD_SS: return RUN_SS % (processName, processPid)
  elif resolutionCmd == CMD_LSOF: return RUN_LSOF % (processName, processPid)
  elif resolutionCmd == CMD_SOCKSTAT: return RUN_SOCKSTAT % (processName, processPid)
  elif resolutionCmd == CMD_BSD_SOCKSTAT: return RUN_BSD_SOCKSTAT % (processName, processPid)
  elif resolutionCmd == CMD_BSD_PROCSTAT: return RUN_BSD_PROCSTAT % processPid
  else: raise ValueError("Unrecognized resolution type: %s" % resolutionCmd)

def getConnections(resolutionCmd, processName, processPid = ""):
  """
  Retrieves a list of the current connections for a given process, providing a
  tuple list of the form:
  [(local_ipAddr1, local_port1, foreign_ipAddr1, foreign_port1), ...]
  this raises an IOError if no connections are available or resolution fails
  (in most cases these appear identical). Common issues include:
    - insufficient permissions
    - resolution command is unavailable
    - usage of the command is non-standard (particularly an issue for BSD)
  
  Arguments:
    resolutionCmd - command to use in resolving the address
    processName   - name of the process for which connections are fetched
    processPid    - process ID (this helps improve accuracy)
  """
  
  
  # raises an IOError if the command fails or isn't available
  cmd = getResolverCommand(resolutionCmd, processName, processPid)
  results = sysTools.call(cmd)
  
  if not results: raise IOError("No results found using: %s" % cmd)
  
  # parses results for the resolution command
  conn = []
  for line in results:
    comp = line.split()
    
    if resolutionCmd == CMD_NETSTAT:
      localIp, localPort = comp[3].split(":")
      foreignIp, foreignPort = comp[4].split(":")
    elif resolutionCmd == CMD_SS:
      localIp, localPort = comp[4].split(":")
      foreignIp, foreignPort = comp[5].split(":")
    elif resolutionCmd == CMD_LSOF:
      local, foreign = comp[8].split("->")
      localIp, localPort = local.split(":")
      foreignIp, foreignPort = foreign.split(":")
    elif resolutionCmd == CMD_SOCKSTAT:
      localIp, localPort = comp[4].split(":")
      foreignIp, foreignPort = comp[5].split(":")
    elif resolutionCmd == CMD_BSD_SOCKSTAT:
      localIp, localPort = comp[5].split(":")
      foreignIp, foreignPort = comp[6].split(":")
    elif resolutionCmd == CMD_BSD_PROCSTAT:
      localIp, localPort = comp[9].split(":")
      foreignIp, foreignPort = comp[10].split(":")
    
    conn.append((localIp, localPort, foreignIp, foreignPort))
  
  return conn

def isResolverAlive(processName, processPid = ""):
  """
  This provides true if a singleton resolver instance exists for the given
  process/pid combination, false otherwise.
  
  Arguments:
    processName - name of the process being checked
    processPid  - pid of the process being checked, if undefined this matches
                  against any resolver with the process name
  """
  
  for resolver in RESOLVERS:
    if not resolver._halt and resolver.processName == processName and (not processPid or resolver.processPid == processPid):
      return True
  
  return False

def getResolver(processName, processPid = ""):
  """
  Singleton constructor for resolver instances. If a resolver already exists
  for the process then it's returned. Otherwise one is created and started.
  
  Arguments:
    processName - name of the process being resolved
    processPid  - pid of the process being resolved, if undefined this matches
                  against any resolver with the process name
  """
  
  # check if one's already been created
  haltedIndex = -1 # old instance of this resolver with the _halt flag set
  for i in range(len(RESOLVERS)):
    resolver = RESOLVERS[i]
    if resolver.processName == processName and (not processPid or resolver.processPid == processPid):
      if resolver._halt and RECREATE_HALTED_RESOLVERS: haltedIndex = i
      else: return resolver
  
  # make a new resolver
  r = ConnectionResolver(processName, processPid)
  r.start()
  
  # overwrites halted instance of this resolver if it exists, otherwise append
  if haltedIndex == -1: RESOLVERS.append(r)
  else: RESOLVERS[haltedIndex] = r
  return r

def getSystemResolvers(osType = None):
  """
  Provides the types of connection resolvers available on this operating
  system.
  
  Arguments:
    osType - operating system type, fetched from the os module if undefined
  """
  
  if osType == None: osType = os.uname()[0]
  if osType == "FreeBSD": return [CMD_BSD_SOCKSTAT, CMD_BSD_PROCSTAT, CMD_LSOF]
  else: return [CMD_NETSTAT, CMD_SOCKSTAT, CMD_LSOF, CMD_SS]

class ConnectionResolver(threading.Thread):
  """
  Service that periodically queries for a process' current connections. This
  provides several benefits over on-demand queries:
  - queries are non-blocking (providing cached results)
  - falls back to use different resolution methods in case of repeated failures
  - avoids overly frequent querying of connection data, which can be demanding
    in terms of system resources
  
  Unless an overriding method of resolution is requested this defaults to
  choosing a resolver the following way:
  
  - Checks the current PATH to determine which resolvers are available. This
    uses the first of the following that's available:
      netstat, ss, lsof (picks netstat if none are found)
  
  - Attempts to resolve using the selection. Single failures are logged at the
    INFO level, and a series of failures at NOTICE. In the later case this
    blacklists the resolver, moving on to the next. If all resolvers fail this
    way then resolution's abandoned and logs a WARN message.
  
  The time between resolving connections, unless overwritten, is set to be
  either five seconds or ten times the runtime of the resolver (whichever is
  larger). This is to prevent systems either strapped for resources or with a
  vast number of connections from being burdened too heavily by this daemon.
  
  Parameters:
    processName       - name of the process being resolved
    processPid        - pid of the process being resolved
    resolveRate       - minimum time between resolving connections (in seconds,
                        None if using the default)
    * defaultRate     - default time between resolving connections
    lastLookup        - time connections were last resolved (unix time, -1 if
                        no resolutions have yet been successful)
    overwriteResolver - method of resolution (uses default if None)
    * defaultResolver - resolver used by default (None if all resolution
                        methods have been exhausted)
    resolverOptions   - resolvers to be cycled through (differ by os)
    
    * read-only
  """
  
  def __init__(self, processName, processPid = "", resolveRate = None):
    """
    Initializes a new resolver daemon. When no longer needed it's suggested
    that this is stopped.
    
    Arguments:
      processName - name of the process being resolved
      processPid  - pid of the process being resolved
      resolveRate - time between resolving connections (in seconds, None if
                    chosen dynamically)
    """
    
    threading.Thread.__init__(self)
    self.setDaemon(True)
    
    self.processName = processName
    self.processPid = processPid
    self.resolveRate = resolveRate
    self.defaultRate = CONFIG["queries.connections.minRate"]
    self.lastLookup = -1
    self.overwriteResolver = None
    self.defaultResolver = CMD_NETSTAT
    
    osType = os.uname()[0]
    self.resolverOptions = getSystemResolvers(osType)
    
    resolverLabels = ", ".join([CMD_STR[option] for option in self.resolverOptions])
    log.log(CONFIG["log.connResolverOptions"], "Operating System: %s, Connection Resolvers: %s" % (osType, resolverLabels))
    
    # sets the default resolver to be the first found in the system's PATH
    # (left as netstat if none are found)
    for resolver in self.resolverOptions:
      if sysTools.isAvailable(CMD_STR[resolver]):
        self.defaultResolver = resolver
        break
    
    self._connections = []        # connection cache (latest results)
    self._isPaused = False
    self._halt = False            # terminates thread if true
    self._cond = threading.Condition()  # used for pausing the thread
    self._subsiquentFailures = 0  # number of failed resolutions with the default in a row
    self._resolverBlacklist = []  # resolvers that have failed to resolve
    
    # Number of sequential times the threshold rate's been too low. This is to
    # avoid having stray spikes up the rate.
    self._rateThresholdBroken = 0
  
  def run(self):
    while not self._halt:
      minWait = self.resolveRate if self.resolveRate else self.defaultRate
      timeSinceReset = time.time() - self.lastLookup
      
      if self._isPaused or timeSinceReset < minWait:
        sleepTime = max(0.2, minWait - timeSinceReset)
        
        self._cond.acquire()
        if not self._halt: self._cond.wait(sleepTime)
        self._cond.release()
        
        continue # done waiting, try again
      
      isDefault = self.overwriteResolver == None
      resolver = self.defaultResolver if isDefault else self.overwriteResolver
      
      # checks if there's nothing to resolve with
      if not resolver:
        self.lastLookup = time.time() # avoids a busy wait in this case
        continue
      
      try:
        resolveStart = time.time()
        connResults = getConnections(resolver, self.processName, self.processPid)
        lookupTime = time.time() - resolveStart
        
        self._connections = connResults
        
        newMinDefaultRate = 100 * lookupTime
        if self.defaultRate < newMinDefaultRate:
          if self._rateThresholdBroken >= 3:
            # adding extra to keep the rate from frequently changing
            self.defaultRate = newMinDefaultRate + 0.5
            
            msg = "connection lookup time increasing to %0.1f seconds per call" % self.defaultRate
            log.log(CONFIG["log.connLookupRateGrowing"], msg)
          else: self._rateThresholdBroken += 1
        else: self._rateThresholdBroken = 0
        
        if isDefault: self._subsiquentFailures = 0
      except IOError, exc:
        # this logs in a couple of cases:
        # - special failures noted by getConnections (most cases are already
        # logged via sysTools)
        # - note fail-overs for default resolution methods
        if str(exc).startswith("No results found using:"):
          log.log(CONFIG["log.connLookupFailed"], str(exc))
        
        if isDefault:
          self._subsiquentFailures += 1
          
          if self._subsiquentFailures >= RESOLVER_FAILURE_TOLERANCE:
            # failed several times in a row - abandon resolver and move on to another
            self._resolverBlacklist.append(resolver)
            self._subsiquentFailures = 0
            
            # pick another (non-blacklisted) resolver
            newResolver = None
            for r in self.resolverOptions:
              if not r in self._resolverBlacklist:
                newResolver = r
                break
            
            if newResolver:
              # provide notice that failures have occurred and resolver is changing
              msg = RESOLVER_SERIAL_FAILURE_MSG % (CMD_STR[resolver], CMD_STR[newResolver])
              log.log(CONFIG["log.connLookupFailover"], msg)
            else:
              # exhausted all resolvers, give warning
              log.log(CONFIG["log.connLookupAbandon"], RESOLVER_FINAL_FAILURE_MSG)
            
            self.defaultResolver = newResolver
      finally:
        self.lastLookup = time.time()
  
  def getConnections(self):
    """
    Provides the last queried connection results, an empty list if resolver
    has been halted.
    """
    
    if self._halt: return []
    else: return list(self._connections)
  
  def setPaused(self, isPause):
    """
    Allows or prevents further connection resolutions (this still makes use of
    cached results).
    
    Arguments:
      isPause - puts a freeze on further resolutions if true, allows them to
                continue otherwise
    """
    
    if isPause == self._isPaused: return
    self._isPaused = isPause
  
  def stop(self):
    """
    Halts further resolutions and terminates the thread.
    """
    
    self._cond.acquire()
    self._halt = True
    self._cond.notifyAll()
    self._cond.release()

