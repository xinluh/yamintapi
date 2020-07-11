import http.server
import socketserver
from urllib.parse import urlparse
from threading import Timer
import logging


logger = logging.getLogger(__name__)


def wait_for_code_via_http(port=8000, timeout=120, url_keyword='mintcode'):
    """
    Start a temporary HTTP server on `port` to wait for http request with url containing `url_keyword`
    and return the query part of the url

    If `timeout` is not None, server will timeout at `timeout` seconds even if no matching url was received.
    """
    socketserver.TCPServer.allow_reuse_address = True
    server = None

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            # Looking for a request like /<url_keyword>?123456
            if url_keyword not in self.path:
                self.send_error(404, '')
                return

            # store data on the server object
            self.server.received_code = urlparse(self.path).query

            self.send_response(200, 'OK')
            self.end_headers()  # needed to close the response

            # obscure way to kill server from the handler without starting separate threads
            self.server._BaseServer__shutdown_request = True

    server = socketserver.TCPServer(("", port), Handler)

    def timeout_kill_server(server):
        logger.info('Killing server due to timeout after ' + str(timeout))
        server.shutdown()

    try:
        if timeout:
            Timer(timeout, timeout_kill_server, args=(server,)).start()

        logger.info("serving at port " + str(port))
        server.serve_forever()
    finally:
        server.shutdown()
        server.socket.close()

    return getattr(server, 'received_code', None)

# To test, uncomment following line and run python <this file.py>:
# print(wait_for_code_via_http(timeout=None, port=2222))
