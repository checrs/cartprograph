import os
import re
import subprocess
import enum
import logging

import tracer.qemu


l = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

QEMU_PATH = tracer.qemu.qemu_path('x86_64')


class RegexReader:
    def __init__(self, fd):
        self.fd = fd
        self._buffer = b''

    def unread(self, data):
        self._buffer = data + self._buffer

    def read_regex(self, *regexs):
        while True:
            matches = [
                re.search(regex, self._buffer)
                for regex in regexs
            ]
            if any(matches):
                match = sorted([match for match in matches if match], key=lambda k: k.start())[0]
                self._buffer = self._buffer[match.end():]
                return match
            self._buffer += os.read(self.fd, 4096)


class TracerEvent(enum.Enum):
    SYSCALL_START = enum.auto()
    SYSCALL_FINISH = enum.auto()
    EXEC_BLOCK = enum.auto()


def on_event(event, filter_):
    def wrapper(func):
        if not hasattr(func, 'on_event'):
            func.on_event = []
        func.on_event.append({
            'event': event,
            'filter': filter_,
        })
        return func
    return wrapper


class Tracer:
    SYSCALL_ENABLED = True
    EXEC_BLOCK_ENABLED = True

    def __init__(self, target_args):
        self.target = target_args

    def dispatch_event(self, event, *, syscall=None, args=None, result=None, addr=None):
        l.debug('Dispatching %s (%s)', event, hex(addr) if addr else syscall)
        for attr_name in dir(self):
            attr = getattr(self, attr_name)
            if hasattr(attr, 'on_event'):
                for handler in attr.on_event:
                    handler_event = handler['event']
                    filter_ = handler['filter']
                    if handler_event != event:
                        continue
                    if event == TracerEvent.SYSCALL_START and re.match('^' + filter_ + '$', syscall):
                        attr(syscall, args)
                    elif event == TracerEvent.SYSCALL_FINISH and re.match('^' + filter_ + '$', syscall):
                        attr(syscall, args, result)
                    elif event == TracerEvent.EXEC_BLOCK and (filter_ is ... or addr in filter_):
                        attr(addr)

    def run(self):
        qemu_log_r, qemu_log_w = os.pipe()
        qemu_log_path = f'/proc/{os.getpid()}/fd/{qemu_log_w}'

        log_options = []
        if self.SYSCALL_ENABLED:
            log_options.append('strace')
        if self.EXEC_BLOCK_ENABLED:
            log_options.append('exec')

        popen = subprocess.Popen([QEMU_PATH, '-d', ','.join(log_options),
                                  '-D', qemu_log_path,
                                  *self.target],
                                 stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE)
        self.popen = popen

        reader = RegexReader(qemu_log_r)

        while True:
            syscall_re = br'(?P<pid>\d+) (?P<syscall>\w+)\((?P<args>.*?)\)'
            syscall_result_re = br' = (?P<result>.*)\n'
            bb_addr_re = br'Trace .*?: .*? \[.*?\/(?P<addr>.*?)\/.*?\] \n'

            match = reader.read_regex(syscall_re, bb_addr_re)

            if match.groupdict().get('addr'):
                bb_addr_match = match
                addr = int(bb_addr_match['addr'], 16)
                self.dispatch_event(TracerEvent.EXEC_BLOCK, addr=addr)

            elif match.groupdict().get('syscall'):
                syscall_match = match
                pid = int(syscall_match['pid'].decode())
                syscall = syscall_match['syscall'].decode()
                args = tuple(syscall_match['args'].decode().split(','))

                self.dispatch_event(TracerEvent.SYSCALL_START, syscall=syscall, args=args)

                if 'exit' in syscall:
                    break

                syscall_result_match = reader.read_regex(syscall_result_re)
                result, _, result_info = syscall_result_match['result'].decode().partition(' ')
                result = int(result, 16) if result.startswith('0x') else int(result)

                self.dispatch_event(TracerEvent.SYSCALL_FINISH, syscall=syscall, args=args, result=result)