/**
 * Python is already required by the SSH update gate. Unlike Darwin nohup this
 * launcher needs no controlling terminal: setsid is performed by Popen, all
 * transport/mutex FDs are closed, and only the actual child's PID is returned.
 * Ignored SIGHUP survives exec; Popen's exec/setup errors propagate to the caller.
 */
export const REMOTE_DETACHED_SHELL_PY = String.raw`
import os,signal,subprocess,sys
signal.signal(signal.SIGHUP,signal.SIG_IGN)
fd=os.open(sys.argv[2],os.O_WRONLY|os.O_APPEND|os.O_CREAT|os.O_CLOEXEC,0o600)
with os.fdopen(fd,'ab',buffering=0) as output:
 child=subprocess.Popen(['sh','-c',sys.argv[1]],stdin=subprocess.DEVNULL,stdout=output,stderr=subprocess.STDOUT,close_fds=True,start_new_session=True)
print(child.pid,flush=True)
`.trim()

/** Kernel argv for SSH ownership proofs. Never shell-split Darwin's lossy ps display. */
export const REMOTE_PROCESS_ARGV_PY = String.raw`
def process_argv(pid):
 if sys.platform=='darwin':
  import ctypes,struct
  libc=ctypes.CDLL(None,use_errno=True)
  mib=(ctypes.c_int*3)(1,49,pid) # CTL_KERN, KERN_PROCARGS2
  size=ctypes.c_size_t(1024*1024)
  buf=ctypes.create_string_buffer(size.value)
  if libc.sysctl(mib,3,buf,ctypes.byref(size),None,0)!=0:
   raise OSError(ctypes.get_errno(),'KERN_PROCARGS2 unavailable')
  raw=buf.raw[:size.value]
  argc=struct.unpack_from('i',raw)[0]
  if argc<1:raise ValueError('missing argv')
  pos=raw.index(b'\0',4)+1 # skip executable path and alignment padding
  while pos<len(raw) and raw[pos]==0:pos+=1
  parts=raw[pos:].split(b'\0')
  if len(parts)<=argc:raise ValueError('truncated argv')
  return [part.decode('utf-8','surrogateescape') for part in parts[:argc]]
 raw=open(f'/proc/{pid}/cmdline','rb').read()
 return [part.decode('utf-8','surrogateescape') for part in raw.split(b'\0') if part]
`.trim()
