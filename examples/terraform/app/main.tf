# Root module 2 of the Argus Terraform example.
#
# This module has its own state. Terraform has no way to know it must run
# after ./network -- nothing links the two states. That ordering is declared
# in ../argus.yaml, and sequencing it is the one thing Argus contributes on
# this backend.

terraform {
  required_version = ">= 1.4"
}

variable "environment" {
  type    = string
  default = "dev"
}

resource "terraform_data" "service" {
  input = "service-${var.environment}"
}

resource "terraform_data" "alarm" {
  input = "alarm on ${terraform_data.service.output}"
}
