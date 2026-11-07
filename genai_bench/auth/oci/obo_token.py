from typing import Any, Dict

import oci

from genai_bench.auth.auth_provider import AuthProvider


class OCIOBOTokenAuth(AuthProvider):
    """OCI Authentication Provider using OBO (On-Behalf-Of) token."""

    def __init__(self, token: str, region: str):
        """Initialize OCI OBO Token Auth Provider.

        Args:
            token (str): The security token for OBO authentication
            region (str): The OCI region (e.g., 'us-ashburn-1')
        """
        self.token = token
        self.region = region
        self._signer = None

    def get_config(self) -> Dict[str, Any]:
        """Get OCI configuration.

        Returns:
            Dict[str, Any]: OCI configuration dictionary with region
        """
        return {
            "region": self.region,
        }

    def get_credentials(self) -> oci.signer.AbstractBaseSigner:
        """Get OCI security token signer.

        Returns:
            oci.signer.AbstractBaseSigner: OCI security token signer

        Raises:
            Exception: If token is invalid
        """
        if self._signer is None:
            # OBO ("on behalf of") delegation is signed by an instance
            # principal signer that adds the opc-obo-token header. The previous
            # implementation built a SecurityTokenSigner with a `region` kwarg,
            # which both takes no such argument and is the wrong signer for
            # this flow, so every OBO run failed at signer construction.
            self._signer = oci.auth.signers.InstancePrincipalsDelegationTokenSigner(
                delegation_token=self.token
            )
        return self._signer
