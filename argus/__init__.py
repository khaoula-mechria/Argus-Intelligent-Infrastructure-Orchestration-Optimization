"""Argus -- deploy, visualise and optimise AWS infrastructure.

Argus reads infrastructure described in CloudFormation or Terraform, discovers
the real dependencies between its deployable units, and deploys the
independent ones in parallel instead of following a fixed order.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
