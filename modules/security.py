import os
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

    def _read_file(self, path):
        if path and os.path.exists(path):
            with open(path, 'rb') as f:
                return f.read()
        elif path:
            logger.error(f"[Security] Warning: Credential file not found at {path}")
        return None

    def get_grpc_credentials(self):
        """Exports credentials for gNMI (HTTP/2 via grpc)."""
        root_certs = self._read_file(self.ca_cert)
        private_key = self._read_file(self.client_key)
        cert_chain = self._read_file(self.client_cert)

        if not private_key and not cert_chain and not root_certs:
            # If the user asks for TLS features, they expect an encrypted secure channel
            if self.skip_verify or self.tls_server_name:
                if self.skip_verify:
                    logger.warning(f"[Security] Python gRPC cannot modify its options - but It will be create an TLS session without any credentials")
            # Return empty credentials anyway
            return grpc.ssl_channel_credentials()

        return grpc.ssl_channel_credentials(
            root_certificates=root_certs,
            private_key=private_key,
            certificate_chain=cert_chain
        )

    def get_grpc_options(self):
        """Exports gRPC channel options about secure connections"""
        options = []

        if self.tls_server_name:
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
        if self.client_key:
            kwargs['key_filename'] = self.client_key
        return kwargs
