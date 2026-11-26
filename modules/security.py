import os
import socket
import ssl
from typing import Optional, Tuple
try:
    import grpc
except ImportError:
    grpc = None

import logging
from config.model import SecurityProfile

logger = logging.getLogger(__name__)


class SecurityModule:
    """
    A universal security profile holding X.509 certificates and keys.
    Exports credentials into formats required by different transport libraries.
    """
    def __init__(self, profile: SecurityProfile):
        # Gracefully handle if profile is a dictionary (from dataclasses.asdict) or an object
        if isinstance(profile, dict):
            self.ca_cert = profile.get('tls_ca', "")
            self.client_cert = profile.get('tls_cert', "")
            self.client_key = profile.get('tls_key', "")
            self.skip_verify = profile.get('skip_verify', False)
            self.tls_server_name = profile.get('tls_server_name', "")
            self.tls_version = profile.get('tls_version',"1.3")
            self.ssh_key = profile.get('ssh_key', "")
        else:
            self.ca_cert = getattr(profile, 'tls_ca', "")
            self.client_cert = getattr(profile, 'tls_cert', "")
            self.client_key = getattr(profile, 'tls_key', "")
            self.skip_verify = getattr(profile, 'skip_verify', False)
            self.tls_server_name = getattr(profile, 'tls_server_name', "")
            self.tls_version = getattr(profile, 'tls_version', "1.3")
            self.ssh_key = getattr(profile, 'ssh_key', "")
        self._peer_tls_cache: dict[str, Tuple[Optional[bytes], Optional[str]]] = {}

    def _read_file(self, path):
        if path and os.path.exists(path):
            with open(path, 'rb') as f:
                return f.read()
        elif path:
            logger.error(f"[Security] Warning: Credential file not found at {path}")
        return None

    @staticmethod
    def _parse_host_port(target: str) -> Tuple[str, int]:
        if target.startswith('['):
            end_bracket = target.find(']')
            if end_bracket != -1:
                host = target[1:end_bracket]
                rest = target[end_bracket + 1:]
                if rest.startswith(':'):
                    return host, int(rest[1:])
                return host, 443
        if ':' in target:
            host, port_str = target.rsplit(':', 1)
            return host, int(port_str)
        return target, 443

    def _fetch_peer_tls_info(self, target: str) -> Tuple[Optional[bytes], Optional[str]]:
        """Fetches the server's leaf certificate PEM and a SAN/CN name for skip-verify TLS."""
        host, port = self._parse_host_port(target)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        if self.client_cert and self.client_key and os.path.exists(self.client_cert) and os.path.exists(self.client_key):
            ctx.load_cert_chain(certfile=self.client_cert, keyfile=self.client_key)

        with socket.create_connection((host, port), timeout=5.0) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                der_cert = ssock.getpeercert(binary_form=True)

        if not der_cert:
            return None, None

        pem_bytes = ssl.DER_cert_to_PEM_cert(der_cert).encode('utf-8')
        server_name: Optional[str] = None
        try:
            from cryptography import x509

            cert = x509.load_der_x509_certificate(der_cert)
            try:
                san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
                dns_names = san.get_values_for_type(x509.DNSName)
                if dns_names:
                    server_name = dns_names[0]
                else:
                    ip_addrs = san.get_values_for_type(x509.IPAddress)
                    if ip_addrs:
                        server_name = str(ip_addrs[0])
            except x509.ExtensionNotFound:
                pass
            if not server_name:
                cn_attrs = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
                if cn_attrs:
                    server_name = cn_attrs[0].value
        except Exception as e:
            logger.debug(f"[Security] Could not parse peer certificate SAN/CN for {target}: {e}")

        return pem_bytes, server_name

    def _get_peer_tls_info(self, target: str) -> Tuple[Optional[bytes], Optional[str]]:
        if target not in self._peer_tls_cache:
            try:
                self._peer_tls_cache[target] = self._fetch_peer_tls_info(target)
            except Exception as e:
                logger.warning(f"[Security] Failed to fetch peer certificate for skip_verify on {target}: {e}")
                self._peer_tls_cache[target] = (None, None)
        return self._peer_tls_cache[target]

    def get_grpc_credentials(self, target: str = ""):
        """Exports credentials for gNMI (HTTP/2 via grpc)."""
        root_certs = self._read_file(self.ca_cert)
        private_key = self._read_file(self.client_key)
        cert_chain = self._read_file(self.client_cert)

        if self.skip_verify and target:
            logger.warning(f"[Security] skip_verify enabled for {target}: trusting presented server certificate without CA validation.")
            root_certs, _ = self._get_peer_tls_info(target)

        if not private_key and not cert_chain and not root_certs:
            return grpc.ssl_channel_credentials()

        return grpc.ssl_channel_credentials(
            root_certificates=root_certs,
            private_key=private_key,
            certificate_chain=cert_chain
        )

    def get_grpc_options(self, target: str = ""):
        """Exports gRPC channel options about secure connections"""
        options = []

        if self.skip_verify and target:
            _, peer_name = self._get_peer_tls_info(target)
            if peer_name:
                options.append(('grpc.ssl_target_name_override', peer_name))
        elif self.tls_server_name:
            options.append(('grpc.ssl_target_name_override', self.tls_server_name))

        # 'tls_version' is meaningless for Python gRPC: it does auto-negotiate via BoringSSL.

        return options if options else None

    def get_requests_kwargs(self):
        """Exports credentials for RESTCONF (HTTPS via requests)."""
        kwargs = {}
        if self.ca_cert:
            kwargs['verify'] = self.ca_cert
        elif self.skip_verify:
            kwargs['verify'] = False
            
        if self.client_cert and self.client_key:
            kwargs['cert'] = (self.client_cert, self.client_key)
        return kwargs

    def get_ssh_kwargs(self):
        """Exports credentials for NETCONF (SSH via ncclient)."""
        kwargs = {}
        key_path = self.ssh_key or self.client_key
        if key_path:
            kwargs['key_filename'] = key_path
        return kwargs
