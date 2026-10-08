"""Producer clients: FinancialPlanning plan/ingestion API and FinanceModel job API.

``platform.PlatformClient`` and ``jobs.JobClient`` speak to any :class:`~finplan_tools.core.transport.Transport`:
``sigv4.SigV4HttpTransport`` in deployed environments, the in-process mocks of the test-only
``finplan_tools_testing`` package offline. Mocks never ship in the deployable artifact.
"""

from .jobs import JobClient
from .platform import PlatformClient

__all__ = ["JobClient", "PlatformClient"]
