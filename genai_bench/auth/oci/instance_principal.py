from typing import Any, Dict, Optional

import oci

from genai_bench.auth.auth_provider import AuthProvider
from genai_bench.logging import init_logger

logger = init_logger(__name__)


class OCIInstancePrincipalAuth(AuthProvider):
    """OCI Authentication Provider using instance principal."""

    def __init__(
        self, security_token: Optional[str] = None, region: Optional[str] = None
    ):
        """Initialize OCI Instance Principal Auth Provider.

        Args:
            security_token (Optional[str]): Security token for authentication
            region (Optional[str]): OCI region
        """
        self._signer = None
        self.region = region
        # Instance principal auth signs with the instance's own certificate;
        # InstancePrincipalsSecurityTokenSigner() takes no token argument, so a
        # --security-token passed alongside instance_principal cannot be used.
        # Keep it so the caller can tell the user instead of dropping it silently.
        self.security_token = security_token

    def get_config(self) -> Dict[str, Any]:
        """Get OCI configuration.

        Returns:
            Dict[str, Any]: OCI configuration dictionary with region and tenancy

        Raises:
            Exception: If instance principal auth is not configured
        """
        signer = self.get_credentials()
        config = {"region": signer.region, "tenancy": signer.tenancy_id}
        if self.region:
            config["region"] = self.region
        return config

    def get_credentials(self) -> oci.signer.AbstractBaseSigner:
        """Get OCI instance principal signer.

        Returns:
            oci.signer.AbstractBaseSigner: OCI instance principal signer

        Raises:
            Exception: If instance principal auth is not configured
        """
        if self._signer is None:
            if self.security_token:
                logger.warning(
                    "--security-token is ignored for instance_principal "
                    "authentication: the instance's own certificate is used to "
                    "sign requests. Use --auth instance_obo_user to delegate with "
                    "a security token instead."
                )
            self._signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
        return self._signer
