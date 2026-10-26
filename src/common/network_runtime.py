"""Release-time TLS contract, shared by build validation and packaged self-tests."""
import platform
import ssl
import sys


def runtime_info():
    return {'python': platform.python_version(), 'openssl': ssl.OPENSSL_VERSION,
            'platform': sys.platform, 'packaged': bool(getattr(sys, 'frozen', False))}


def require_release_tls():
    # The v1.9.1 macOS bundle used OpenSSL 3.0.16 while development used 3.5.8.
    # An isolated A/B reproduced Reddit 403 vs 200 with the TLS module changed.
    # Keep the tested runtime family in release artifacts; this is not a promise
    # that a remote platform will accept every future request.
    if ssl.OPENSSL_VERSION_INFO < (3, 5, 0):
        raise RuntimeError('Release requires OpenSSL >= 3.5.0; use the managed Python build environment')
    return runtime_info()
