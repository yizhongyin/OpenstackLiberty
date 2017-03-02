import errno
import functools
import os
import sys
import time
import traceback
import types
import warnings

from eventlet import greenio
from eventlet import greenpool
from eventlet import support
from eventlet.green import BaseHTTPServer
from eventlet.green import socket
from eventlet.support import six
from eventlet.support.six.moves import urllib


DEFAULT_MAX_SIMULTANEOUS_REQUESTS = 1024
DEFAULT_MAX_HTTP_VERSION = 'HTTP/1.1'
MAX_REQUEST_LINE = 8192
MAX_HEADER_LINE = 8192
MAX_TOTAL_HEADER_SIZE = 65536
MINIMUM_CHUNK_SIZE = 4096
# %(client_port)s is also available
DEFAULT_LOG_FORMAT = ('%(client_ip)s - - [%(date_time)s] "%(request_line)s"'
                      ' %(status_code)s %(body_length)s %(wall_seconds).6f')
is_accepting = True

__all__ = ['server', 'format_date_time']

# Weekday and month names for HTTP date/time formatting; always English!
_weekdayname = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_monthname = [None,  # Dummy so we can use 1-based month numbers
              "Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def format_date_time(timestamp):
    """Formats a unix timestamp into an HTTP standard string."""
    year, month, day, hh, mm, ss, wd, _y, _z = time.gmtime(timestamp)
    return "%s, %02d %3s %4d %02d:%02d:%02d GMT" % (
        _weekdayname[wd], day, _monthname[month], year, hh, mm, ss
    )


# Collections of error codes to compare against.  Not all attributes are set
# on errno module on all platforms, so some are literals :(
BAD_SOCK = set((errno.EBADF, 10053))
BROKEN_SOCK = set((errno.EPIPE, errno.ECONNRESET))


class ChunkReadError(ValueError):
    pass


# special flag return value for apps
class _AlreadyHandled(object):

    def __iter__(self):
        return self

    def next(self):
        raise StopIteration

    __next__ = next


ALREADY_HANDLED = _AlreadyHandled()


class Input(object):

    def __init__(self,
                 rfile,
                 content_length,
                 sock,
                 wfile=None,
                 wfile_line=None,
                 chunked_input=False):

        self.rfile = rfile
        self._sock = sock
        if content_length is not None:
            content_length = int(content_length)
        self.content_length = content_length

        self.wfile = wfile
        self.wfile_line = wfile_line

        self.position = 0
        self.chunked_input = chunked_input
        self.chunk_length = -1

        # (optional) headers to send with a "100 Continue" response. Set by
        # calling set_hundred_continue_respose_headers() on env['wsgi.input']
        self.hundred_continue_headers = None
        self.is_hundred_continue_response_sent = False

    def send_hundred_continue_response(self):
        towrite = []

        # 100 Continue status line
        towrite.append(self.wfile_line)

        # Optional headers
        if self.hundred_continue_headers is not None:
            # 100 Continue headers
            for header in self.hundred_continue_headers:
                towrite.append(six.b('%s: %s\r\n' % header))

        # Blank line
        towrite.append(b'\r\n')

        self.wfile.writelines(towrite)
        self.wfile.flush()

        # Reinitialize chunk_length (expect more data)
        self.chunk_length = -1

    def _do_read(self, reader, length=None):
        if self.wfile is not None and not self.is_hundred_continue_response_sent:
            # 100 Continue response
            self.send_hundred_continue_response()
            self.is_hundred_continue_response_sent = True
        if (self.content_length is not None) and (
                length is None or length > self.content_length - self.position):
            length = self.content_length - self.position
        if not length:
            return b''
        try:
            read = reader(length)
        except greenio.SSL.ZeroReturnError:
            read = b''
        self.position += len(read)
        return read

    def _chunked_read(self, rfile, length=None, use_readline=False):
        if self.wfile is not None and not self.is_hundred_continue_response_sent:
            # 100 Continue response
            self.send_hundred_continue_response()
            self.is_hundred_continue_response_sent = True
        try:
            if length == 0:
                return ""

            if length and length < 0:
                length = None

            if use_readline:
                reader = self.rfile.readline
            else:
                reader = self.rfile.read

            response = []
            while self.chunk_length != 0:
                maxreadlen = self.chunk_length - self.position
                if length is not None and length < maxreadlen:
                    maxreadlen = length

                if maxreadlen > 0:
                    data = reader(maxreadlen)
                    if not data:
                        self.chunk_length = 0
                        raise IOError("unexpected end of file while parsing chunked data")

                    datalen = len(data)
                    response.append(data)

                    self.position += datalen
                    if self.chunk_length == self.position:
                        rfile.readline()

                    if length is not None:
                        length -= datalen
                        if length == 0:
                            break
                    if use_readline and data[-1] == "\n":
                        break
                else:
                    try:
                        self.chunk_length = int(rfile.readline().split(b";", 1)[0], 16)
                    except ValueError as err:
                        raise ChunkReadError(err)
                    self.position = 0
                    if self.chunk_length == 0:
                        rfile.readline()
        except greenio.SSL.ZeroReturnError:
            pass
        return b''.join(response)

    def read(self, length=None):
        if self.chunked_input:
            return self._chunked_read(self.rfile, length)
        return self._do_read(self.rfile.read, length)

    def readline(self, size=None):
        if self.chunked_input:
            return self._chunked_read(self.rfile, size, True)
        else:
            return self._do_read(self.rfile.readline, size)

    def readlines(self, hint=None):
        return self._do_read(self.rfile.readlines, hint)

    def __iter__(self):
        return iter(self.read, b'')

    def get_socket(self):
        return self._sock

    def set_hundred_continue_response_headers(self, headers,
                                              capitalize_response_headers=True):
        # Response headers capitalization (default)
        # CONTent-TYpe: TExt/PlaiN -> Content-Type: TExt/PlaiN
        # Per HTTP RFC standard, header name is case-insensitive.
        # Please, fix your client to ignore header case if possible.
        if capitalize_response_headers:
            headers = [
                ('-'.join([x.capitalize() for x in key.split('-')]), value)
                for key, value in headers]
        self.hundred_continue_headers = headers

    def discard(self, buffer_size=16 << 10):
        while self.read(buffer_size):
            pass


class HeaderLineTooLong(Exception):
    pass


class HeadersTooLarge(Exception):
    pass


def get_logger(log, debug):
    if callable(getattr(log, 'info', None)) \
       and callable(getattr(log, 'debug', None)):
        return log
    else:
        return LoggerFileWrapper(log, debug)


class LoggerFileWrapper(object):
    def __init__(self, log, debug):
        self.log = log
        self._debug = debug

    def error(self, msg, *args, **kwargs):
        self.write(msg, *args)

    def info(self, msg, *args, **kwargs):
        self.write(msg, *args)

    def debug(self, msg, *args, **kwargs):
        if self._debug:
            self.write(msg, *args)

    def write(self, msg, *args):
        msg = msg + '\n'
        if args:
            msg = msg % args
        self.log.write(msg)


class FileObjectForHeaders(object):

    def __init__(self, fp):
        self.fp = fp
        self.total_header_size = 0

    def readline(self, size=-1):
        sz = size
        if size < 0:
            sz = MAX_HEADER_LINE
        rv = self.fp.readline(sz)
        if len(rv) >= MAX_HEADER_LINE:
            raise HeaderLineTooLong()
        self.total_header_size += len(rv)
        if self.total_header_size > MAX_TOTAL_HEADER_SIZE:
            raise HeadersTooLarge()
        return rv


class HttpProtocol(BaseHTTPServer.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    minimum_chunk_size = MINIMUM_CHUNK_SIZE
    capitalize_response_headers = True

    # https://github.com/eventlet/eventlet/issues/295
    # Stdlib default is 0 (unbuffered), but then `wfile.writelines()` looses data
    # so before going back to unbuffered, remove any usage of `writelines`.
    wbufsize = 16 << 10

    def setup(self):
        # overriding SocketServer.setup to correctly handle SSL.Connection objects
        conn = self.connection = self.request

        # TCP_QUICKACK is a better alternative to disabling Nagle's algorithm
        # https://news.ycombinator.com/item?id=10607422
        if getattr(socket, 'TCP_QUICKACK', None):
            try:
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_QUICKACK, True)
            except socket.error:
                pass

        try:
            self.rfile = conn.makefile('rb', self.rbufsize)
            self.wfile = conn.makefile('wb', self.wbufsize)
        except (AttributeError, NotImplementedError):
            if hasattr(conn, 'send') and hasattr(conn, 'recv'):
                # it's an SSL.Connection
                self.rfile = socket._fileobject(conn, "rb", self.rbufsize)
                self.wfile = socket._fileobject(conn, "wb", self.wbufsize)
            else:
                # it's a SSLObject, or a martian
                raise NotImplementedError("wsgi.py doesn't support sockets "
                                          "of type %s" % type(conn))

    def handle_one_request(self):
        if self.server.max_http_version:
            self.protocol_version = self.server.max_http_version

        if self.rfile.closed:
            self.close_connection = 1
            return

        try:
            self.raw_requestline = self.rfile.readline(self.server.url_length_limit)
            if len(self.raw_requestline) == self.server.url_length_limit:
                self.wfile.write(
                    b"HTTP/1.0 414 Request URI Too Long\r\n"
                    b"Connection: close\r\nContent-length: 0\r\n\r\n")
                self.close_connection = 1
                return
        except greenio.SSL.ZeroReturnError:
            self.raw_requestline = ''
        except socket.error as e:
            if support.get_errno(e) not in BAD_SOCK:
                raise
            self.raw_requestline = ''

        if not self.raw_requestline:
            self.close_connection = 1
            return

        orig_rfile = self.rfile
        try:
            self.rfile = FileObjectForHeaders(self.rfile)
            if not self.parse_request():
                return
        except HeaderLineTooLong:
            self.wfile.write(
                b"HTTP/1.0 400 Header Line Too Long\r\n"
                b"Connection: close\r\nContent-length: 0\r\n\r\n")
            self.close_connection = 1
            return
        except HeadersTooLarge:
            self.wfile.write(
                b"HTTP/1.0 400 Headers Too Large\r\n"
                b"Connection: close\r\nContent-length: 0\r\n\r\n")
            self.close_connection = 1
            return
        finally:
            self.rfile = orig_rfile

        content_length = self.headers.get('content-length')
        if content_length:
            try:
                int(content_length)
            except ValueError:
                self.wfile.write(
                    b"HTTP/1.0 400 Bad Request\r\n"
                    b"Connection: close\r\nContent-length: 0\r\n\r\n")
                self.close_connection = 1
                return

        self.environ = self.get_environ()
        self.application = self.server.app
        try:
            self.server.outstanding_requests += 1
            try:
                self.handle_one_response()
            except socket.error as e:
                # Broken pipe, connection reset by peer
                if support.get_errno(e) not in BROKEN_SOCK:
                    raise
        finally:
            self.server.outstanding_requests -= 1

    def handle_one_response(self):
        start = time.time()
        headers_set = []
        headers_sent = []

        wfile = self.wfile
        result = None
        use_chunked = [False]
        length = [0]
        status_code = [200]

        def write(data):
            towrite = []
            if not headers_set:
                raise AssertionError("write() before start_response()")
            elif not headers_sent:
                status, response_headers = headers_set
                headers_sent.append(1)
                header_list = [header[0].lower() for header in response_headers]
                towrite.append(six.b('%s %s\r\n' % (self.protocol_version, status)))
                for header in response_headers:
                    towrite.append(six.b('%s: %s\r\n' % header))

                # send Date header?
                if 'date' not in header_list:
                    towrite.append(six.b('Date: %s\r\n' % (format_date_time(time.time()),)))

                client_conn = self.headers.get('Connection', '').lower()
                send_keep_alive = False
                if self.close_connection == 0 and \
                   self.server.keepalive and (client_conn == 'keep-alive' or
                                              (self.request_version == 'HTTP/1.1' and
                                               not client_conn == 'close')):
                        # only send keep-alives back to clients that sent them,
                        # it's redundant for 1.1 connections
                    send_keep_alive = (client_conn == 'keep-alive')
                    self.close_connection = 0
                else:
                    self.close_connection = 1

                if 'content-length' not in header_list:
                    if self.request_version == 'HTTP/1.1':
                        use_chunked[0] = True
                        towrite.append(b'Transfer-Encoding: chunked\r\n')
                    elif 'content-length' not in header_list:
                        # client is 1.0 and therefore must read to EOF
                        self.close_connection = 1

                if self.close_connection:
                    towrite.append(b'Connection: close\r\n')
                elif send_keep_alive:
                    towrite.append(b'Connection: keep-alive\r\n')
                towrite.append(b'\r\n')
                # end of header writing

            if use_chunked[0]:
                # Write the chunked encoding
                towrite.append(six.b("%x" % (len(data),)) + b"\r\n" + data + b"\r\n")
            else:
                towrite.append(data)
            wfile.writelines(towrite)
            wfile.flush()
            length[0] = length[0] + sum(map(len, towrite))

        def start_response(status, response_headers, exc_info=None):
            status_code[0] = status.split()[0]
            if exc_info:
                try:
                    if headers_sent:
                        # Re-raise original exception if headers sent
                        six.reraise(exc_info[0], exc_info[1], exc_info[2])
                finally:
                    # Avoid dangling circular ref
                    exc_info = None

            # Response headers capitalization
            # CONTent-TYpe: TExt/PlaiN -> Content-Type: TExt/PlaiN
            # Per HTTP RFC standard, header name is case-insensitive.
            # Please, fix your client to ignore header case if possible.
            if self.capitalize_response_headers:
                response_headers = [
                    ('-'.join([x.capitalize() for x in key.split('-')]), value)
                    for key, value in response_headers]

            headers_set[:] = [status, response_headers]
            return write

        try:
            try:
                result = self.application(self.environ, start_response)
                if (isinstance(result, _AlreadyHandled)
                        or isinstance(getattr(result, '_obj', None), _AlreadyHandled)):
                    self.close_connection = 1
                    return

                # Set content-length if possible
                if not headers_sent and hasattr(result, '__len__') and \
                        'Content-Length' not in [h for h, _v in headers_set[1]]:
                    headers_set[1].append(('Content-Length', str(sum(map(len, result)))))

                towrite = []
                towrite_size = 0
                just_written_size = 0
                minimum_write_chunk_size = int(self.environ.get(
                    'eventlet.minimum_write_chunk_size', self.minimum_chunk_size))
                for data in result:
                    if len(data) == 0:
                        continue
                    if isinstance(data, six.text_type):
                        data = data.encode('ascii')

                    towrite.append(data)
                    towrite_size += len(data)
                    if towrite_size >= minimum_write_chunk_size:
                        write(b''.join(towrite))
                        towrite = []
                        just_written_size = towrite_size
                        towrite_size = 0
                if towrite:
                    just_written_size = towrite_size
                    write(b''.join(towrite))
                if not headers_sent or (use_chunked[0] and just_written_size):
                    write(b'')
            except Exception:
                self.close_connection = 1
                tb = traceback.format_exc()
                self.server.log.info(tb)
                if not headers_sent:
                    err_body = six.b(tb) if self.server.debug else b''
                    start_response("500 Internal Server Error",
                                   [('Content-type', 'text/plain'),
                                    ('Content-length', len(err_body))])
                    write(err_body)
        finally:
            if hasattr(result, 'close'):
                result.close()
            request_input = self.environ['eventlet.input']
            if (request_input.chunked_input or
                    request_input.position < (request_input.content_length or 0)):
                # Read and discard body if there was no pending 100-continue
                if not request_input.wfile and self.close_connection == 0:
                    try:
                        request_input.discard()
                    except ChunkReadError as e:
                        self.close_connection = 1
                        self.server.log.error((
                            'chunked encoding error while discarding request body.'
                            + ' ip={0} request="{1}" error="{2}"').format(
                                self.get_client_ip(), self.requestline, e,
                        ))
            finish = time.time()

            for hook, args, kwargs in self.environ['eventlet.posthooks']:
                hook(self.environ, *args, **kwargs)

            if self.server.log_output:
                self.server.log.info(self.server.log_format % {
                    'client_ip': self.get_client_ip(),
                    'client_port': self.client_address[1],
                    'date_time': self.log_date_time_string(),
                    'request_line': self.requestline,
                    'status_code': status_code[0],
                    'body_length': length[0],
                    'wall_seconds': finish - start,
                })

    def get_client_ip(self):
        client_ip = self.client_address[0]
        if self.server.log_x_forwarded_for:
            forward = self.headers.get('X-Forwarded-For', '').replace(' ', '')
            if forward:
                client_ip = "%s,%s" % (forward, client_ip)
        return client_ip

    def get_environ(self):
        env = self.server.get_environ()
        env['REQUEST_METHOD'] = self.command
        env['SCRIPT_NAME'] = ''

        pq = self.path.split('?', 1)
        env['RAW_PATH_INFO'] = pq[0]
        env['PATH_INFO'] = urllib.parse.unquote(pq[0])
        if len(pq) > 1:
            env['QUERY_STRING'] = pq[1]

        ct = self.headers.get('content-type')
        if ct is None:
            try:
                ct = self.headers.type
            except AttributeError:
                ct = self.headers.get_content_type()
        env['CONTENT_TYPE'] = ct

        length = self.headers.get('content-length')
        if length:
            env['CONTENT_LENGTH'] = length
        env['SERVER_PROTOCOL'] = 'HTTP/1.0'

        host, port = self.request.getsockname()[:2]
        env['SERVER_NAME'] = host
        env['SERVER_PORT'] = str(port)
        env['REMOTE_ADDR'] = self.client_address[0]
        env['REMOTE_PORT'] = str(self.client_address[1])
        env['GATEWAY_INTERFACE'] = 'CGI/1.1'

        try:
            headers = self.headers.headers
        except AttributeError:
            headers = self.headers._headers
        else:
            headers = [h.split(':', 1) for h in headers]

        env['headers_raw'] = headers_raw = tuple((k, v.strip()) for k, v in headers)
        for k, v in headers_raw:
            k = k.replace('-', '_').upper()
            if k in env:
                continue
            envk = 'HTTP_' + k
            if envk in env:
                env[envk] += ',' + v
            else:
                env[envk] = v

        if env.get('HTTP_EXPECT') == '100-continue':
            wfile = self.wfile
            wfile_line = b'HTTP/1.1 100 Continue\r\n'
        else:
            wfile = None
            wfile_line = None
        chunked = env.get('HTTP_TRANSFER_ENCODING', '').lower() == 'chunked'
        env['wsgi.input'] = env['eventlet.input'] = Input(
            self.rfile, length, self.connection, wfile=wfile, wfile_line=wfile_line,
            chunked_input=chunked)
        env['eventlet.posthooks'] = []

        return env

    def finish(self):
        try:
            BaseHTTPServer.BaseHTTPRequestHandler.finish(self)
        except socket.error as e:
            # Broken pipe, connection reset by peer
            if support.get_errno(e) not in BROKEN_SOCK:
                raise
        greenio.shutdown_safe(self.connection)
        self.connection.close()

    def handle_expect_100(self):
        return True


class Server(BaseHTTPServer.HTTPServer):

    def __init__(self,
                 socket,
                 address,
                 app,
                 log=None,
                 environ=None,
                 max_http_version=None,
                 protocol=HttpProtocol,
                 minimum_chunk_size=None,
                 log_x_forwarded_for=True,
                 keepalive=True,
                 log_output=True,
                 log_format=DEFAULT_LOG_FORMAT,
                 url_length_limit=MAX_REQUEST_LINE,
                 debug=True,
                 socket_timeout=None,
                 capitalize_response_headers=True):

        self.outstanding_requests = 0
        self.socket = socket
        self.address = address
        if log:
            self.log = get_logger(log, debug)
        else:
            self.log = get_logger(sys.stderr, debug)
        self.app = app
        self.keepalive = keepalive
        self.environ = environ
        self.max_http_version = max_http_version
        self.protocol = protocol
        self.pid = os.getpid()
        self.minimum_chunk_size = minimum_chunk_size
        self.log_x_forwarded_for = log_x_forwarded_for
        self.log_output = log_output
        self.log_format = log_format
        self.url_length_limit = url_length_limit
        self.debug = debug
        self.socket_timeout = socket_timeout
        self.capitalize_response_headers = capitalize_response_headers

        if not self.capitalize_response_headers:
            warnings.warn("""capitalize_response_headers is disabled.
 Please, make sure you know what you are doing.
 HTTP headers names are case-insensitive per RFC standard.
 Most likely, you need to fix HTTP parsing in your client software.""",
                          DeprecationWarning, stacklevel=3)

    def get_environ(self):
        d = {
            'wsgi.errors': sys.stderr,
            'wsgi.version': (1, 0),
            'wsgi.multithread': True,
            'wsgi.multiprocess': False,
            'wsgi.run_once': False,
            'wsgi.url_scheme': 'http',
        }
        # detect secure socket
        if hasattr(self.socket, 'do_handshake'):
            d['wsgi.url_scheme'] = 'https'
            d['HTTPS'] = 'on'
        if self.environ is not None:
            d.update(self.environ)
        return d

    def process_request(self, sock_params):
        # The actual request handling takes place in __init__, so we need to
        # set minimum_chunk_size before __init__ executes and we don't want to modify
        # class variable
        sock, address = sock_params
        proto = new(self.protocol)
        if self.minimum_chunk_size is not None:
            proto.minimum_chunk_size = self.minimum_chunk_size
        proto.capitalize_response_headers = self.capitalize_response_headers
        try:
            proto.__init__(sock, address, self)
        except socket.timeout:
            # Expected exceptions are not exceptional
            sock.close()
            # similar to logging "accepted" in server()
            self.log.debug('(%s) timed out %r' % (self.pid, address))

    def log_message(self, message):
        warnings.warn('server.log_message is deprecated.  Please use server.log.info instead')
        self.log.info(message)


try:
    new = types.InstanceType
except AttributeError:
    new = lambda cls: cls.__new__(cls)


try:
    import ssl
    ACCEPT_EXCEPTIONS = (socket.error, ssl.SSLError)
    ACCEPT_ERRNO = set((errno.EPIPE, errno.EBADF, errno.ECONNRESET,
                        ssl.SSL_ERROR_EOF, ssl.SSL_ERROR_SSL))
except ImportError:
    ACCEPT_EXCEPTIONS = (socket.error,)
    ACCEPT_ERRNO = set((errno.EPIPE, errno.EBADF, errno.ECONNRESET))


def socket_repr(sock):
    scheme = 'http'
    if hasattr(sock, 'do_handshake'):
        scheme = 'https'

    name = sock.getsockname()
    if sock.family == socket.AF_INET:
        hier_part = '//{0}:{1}'.format(*name)
    elif sock.family == socket.AF_INET6:
        hier_part = '//[{0}]:{1}'.format(*name[:2])
    elif sock.family == socket.AF_UNIX:
        hier_part = name
    else:
        hier_part = repr(name)

    return scheme + ':' + hier_part


def server(sock, site,
           log=None,
           environ=None,
           max_size=None,
           max_http_version=DEFAULT_MAX_HTTP_VERSION,
           protocol=HttpProtocol,
           server_event=None,
           minimum_chunk_size=None,
           log_x_forwarded_for=True,
           custom_pool=None,
           keepalive=True,
           log_output=True,
           log_format=DEFAULT_LOG_FORMAT,
           url_length_limit=MAX_REQUEST_LINE,
           debug=True,
           socket_timeout=None,
           capitalize_response_headers=True):
    """Start up a WSGI server handling requests from the supplied server
    socket.  This function loops forever.  The *sock* object will be
    closed after server exits, but the underlying file descriptor will
    remain open, so if you have a dup() of *sock*, it will remain usable.

    .. warning::

        At the moment :func:`server` will always wait for active connections to finish before
        exiting, even if there's an exception raised inside it
        (*all* exceptions are handled the same way, including :class:`greenlet.GreenletExit`
        and those inheriting from `BaseException`).

        While this may not be an issue normally, when it comes to long running HTTP connections
        (like :mod:`eventlet.websocket`) it will become problematic and calling
        :meth:`~eventlet.greenthread.GreenThread.wait` on a thread that runs the server may hang,
        even after using :meth:`~eventlet.greenthread.GreenThread.kill`, as long
        as there are active connections.

    :param sock: Server socket, must be already bound to a port and listening.
    :param site: WSGI application function.
    :param log: logging.Logger instance or file-like object that logs should be written to.
                If a Logger instance is supplied, messages are sent to the INFO log level.
                If not specified, sys.stderr is used.
    :param environ: Additional parameters that go into the environ dictionary of every request.
    :param max_size: Maximum number of client connections opened at any time by this server.
                Default is 1024.
    :param max_http_version: Set to "HTTP/1.0" to make the server pretend it only supports HTTP 1.0.
                This can help with applications or clients that don't behave properly using HTTP 1.1.
    :param protocol: Protocol class.  Deprecated.
    :param server_event: Used to collect the Server object.  Deprecated.
    :param minimum_chunk_size: Minimum size in bytes for http chunks.  This can be used to improve
                performance of applications which yield many small strings, though
                using it technically violates the WSGI spec. This can be overridden
                on a per request basis by setting environ['eventlet.minimum_write_chunk_size'].
    :param log_x_forwarded_for: If True (the default), logs the contents of the x-forwarded-for
                header in addition to the actual client ip address in the 'client_ip' field of the
                log line.
    :param custom_pool: A custom GreenPool instance which is used to spawn client green threads.
                If this is supplied, max_size is ignored.
    :param keepalive: If set to False, disables keepalives on the server; all connections will be
                closed after serving one request.
    :param log_output: A Boolean indicating if the server will log data or not.
    :param log_format: A python format string that is used as the template to generate log lines.
                The following values can be formatted into it: client_ip, date_time, request_line,
                status_code, body_length, wall_seconds.  The default is a good example of how to
                use it.
    :param url_length_limit: A maximum allowed length of the request url. If exceeded, 414 error
                is returned.
    :param debug: True if the server should send exception tracebacks to the clients on 500 errors.
                If False, the server will respond with empty bodies.
    :param socket_timeout: Timeout for client connections' socket operations. Default None means
                wait forever.
    :param capitalize_response_headers: Normalize response headers' names to Foo-Bar.
                Default is True.
    """
    serv = Server(sock, sock.getsockname(),
                  site, log,
                  environ=environ,
                  max_http_version=max_http_version,
                  protocol=protocol,
                  minimum_chunk_size=minimum_chunk_size,
                  log_x_forwarded_for=log_x_forwarded_for,
                  keepalive=keepalive,
                  log_output=log_output,
                  log_format=log_format,
                  url_length_limit=url_length_limit,
                  debug=debug,
                  socket_timeout=socket_timeout,
                  capitalize_response_headers=capitalize_response_headers,
                  )
    if server_event is not None:
        server_event.send(serv)
    if max_size is None:
        max_size = DEFAULT_MAX_SIMULTANEOUS_REQUESTS
    if custom_pool is not None:
        pool = custom_pool
    else:
        pool = greenpool.GreenPool(max_size)
    try:
        serv.log.info("(%s) wsgi starting up on %s" % (
            serv.pid, socket_repr(sock)))
        while is_accepting:
            try:
                client_socket = sock.accept()
                client_socket[0].settimeout(serv.socket_timeout)
                serv.log.debug("(%s) accepted %r" % (
                    serv.pid, client_socket[1]))
                try:
                    pool.spawn_n(serv.process_request, client_socket)
                except AttributeError:
                    warnings.warn("wsgi's pool should be an instance of "
                                  "eventlet.greenpool.GreenPool, is %s. Please convert your"
                                  " call site to use GreenPool instead" % type(pool),
                                  DeprecationWarning, stacklevel=2)
                    pool.execute_async(serv.process_request, client_socket)
            except ACCEPT_EXCEPTIONS as e:
                if support.get_errno(e) not in ACCEPT_ERRNO:
                    raise
            except (KeyboardInterrupt, SystemExit):
                serv.log.info("wsgi exiting")
                break
    finally:
        pool.waitall()
        serv.log.info("(%s) wsgi exited, is_accepting=%s" % (
            serv.pid, is_accepting))
        try:
            # NOTE: It's not clear whether we want this to leave the
            # socket open or close it.  Use cases like Spawning want
            # the underlying fd to remain open, but if we're going
            # that far we might as well not bother closing sock at
            # all.
            sock.close()
        except socket.error as e:
            if support.get_errno(e) not in BROKEN_SOCK:
                traceback.print_exc()