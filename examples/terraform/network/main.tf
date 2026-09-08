# Root module 1 of the Argus Terraform example.
#
# It uses terraform_data, a built-in resource that needs no provider and no
# cloud account, so the example can actually be run: `argus deploy
# examples/terraform --backend terraform` really invokes terraform here.
#
# Inside this module Terraform resolves the order between these three
# resources on its own and creates the two subnets in parallel. Argus does not
# touch that -- see the README, "How it works per backend".

terraform {
  required_version = ">= 1.4"
}

resource "terraform_data" "vpc" {
  input = "vpc-10.0.0.0/16"
}

resource "terraform_data" "subnet_a" {
  input = "subnet-a of ${terraform_data.vpc.output}"
}

resource "terraform_data" "subnet_b" {
  input = "subnet-b of ${terraform_data.vpc.output}"
}

output "vpc_id" {
  value = terraform_data.vpc.output
}
