# Phase 5 — EC2 inference host in Tokyo

This phase takes the immutable image produced in Phase 4 and runs it on one
Amazon EC2 instance in `ap-northeast-1`. It is intentionally private at this
checkpoint: the instance has no inbound security-group rules and the API is
published only on host loopback. Public HTTPS, DNS and a controlled ingress
path belong to the next phase.

Last updated: **2026-09-23**.

## Status

**Automation ready; AWS provisioning blocked at the permission preflight.**

No EC2 instance, security group, instance role or instance profile was created
on 2026-09-23. `AWS_PROFILE=admin` still resolves to
`arn:aws:iam::955519187689:user/quantitative-pipeline-user`, but that identity
is no longer allowed to call `ec2:DescribeVpcs` or inspect its IAM policies.
This is consistent with the temporary `AdministratorAccess` policy having been
removed after Phase 4. Removing it was the correct steady-state action; Phase 5
now needs a short-lived bootstrap permission set or a separate administrator
identity.

The repository contains the complete, syntax-checked deployment path:

| File | Responsibility |
|---|---|
| `infra/iam/apply-ec2-ecr-pull.sh` | Create/update the ECR-pull-only role and instance profile |
| `infra/iam/policy-ecr-pull.json` | Runtime permissions for exactly one ECR repository |
| `infra/iam/policy-phase5-provisioner.json` | Temporary permissions needed by the human/CLI doing the provisioning |
| `infra/ec2/launch-inference.sh` | Discover Tokyo defaults, create a no-ingress SG and launch exactly one instance |
| `infra/ec2/user-data.sh` | Install Docker, pull the digest, start and exercise the API |
| `infra/ec2/describe-inference.sh` | Read-only configuration and console-evidence collector |
| `infra/ec2/terminate-inference.sh` | Guarded teardown of the named Phase-5 instance |

## Architecture and credential flow

```text
human bootstrap identity (temporary provisioning permissions)
  ├─ creates EC2 role + instance profile
  ├─ creates no-ingress security group
  └─ launches one EC2 instance

EC2 host (Amazon Linux 2023, x86_64, Tokyo)
  └─ instance profile → short-lived role credentials
       └─ ECR login + pull from epex-forecaster only
            └─ exact image digest
                 └─ non-root/read-only FastAPI container
                      └─ 127.0.0.1:8000 only
```

There are two permission planes and they must not be confused:

- the **provisioner** may create the named role and EC2 resources temporarily;
- the **instance role** can only obtain an ECR token and pull three layer/image
  operations from `epex-forecaster`.

The instance role cannot manage EC2, push an image, read S3, access a database,
or contact MLflow. No access key or secret is copied into EC2 or Docker.

## Fixed release identity

```text
955519187689.dkr.ecr.ap-northeast-1.amazonaws.com/epex-forecaster
@sha256:2e65ee5f6ce3d26d9bec3ed6e02852278a570c9bb09a37dfaf1838971be126c0
```

The launch command accepts only a `sha256:` value with 64 lowercase hexadecimal
characters. It never deploys `:bundled` or another movable tag.

## Security decisions

| Control | Phase-5 implementation |
|---|---|
| Inbound network | No security-group ingress rules |
| API binding | `127.0.0.1:8000`, not the public interface |
| SSH | No port 22 and no key pair |
| Host credentials | Instance profile with temporary credentials |
| Container credentials | None; metadata hop limit 1 prevents the container from reaching IMDS |
| Metadata | IMDSv2 required; IMDSv1 disabled |
| Image | Exact ECR digest, not a tag |
| Container user | Non-root `app` user baked into the image |
| Container filesystem | Read-only; no mounts or volumes |
| Linux privileges | All capabilities dropped; `no-new-privileges` |
| Root volume | 12 GiB encrypted gp3, deleted on instance termination |
| Duplicate protection | Launch refuses if a non-terminated Phase-5 instance already exists |

The EC2 host necessarily has a root EBS volume for its operating system, Docker
engine and image layers. “No local disk” in the serving contract means the
model is not supplied through an independently managed host path or persistent
data volume. The model remains part of the immutable image, and deleting the
instance deletes its root volume.

## Manual permission step

Use a separate bootstrap administrator if available. Otherwise, from an AWS
root/administrator console session, attach a temporary inline policy to the
CLI identity using `infra/iam/policy-phase5-provisioner.json`, replacing
`__ACCOUNT_ID__` with `955519187689`. The policy grants only the IAM, EC2 and
public AMI-parameter actions used by these scripts. Remove it after the live
evidence is captured.

Temporarily reattaching AWS `AdministratorAccess` would also unblock the work,
but it is substantially broader and is not required by the design.

## Commands to complete after permission is granted

```bash
cd /Users/sarthakgupta/GENAI/epex-price-forecaster
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
export AWS_PROFILE=admin
export AWS_DEFAULT_REGION=ap-northeast-1

./infra/iam/apply-ec2-ecr-pull.sh

IMAGE_DIGEST=sha256:2e65ee5f6ce3d26d9bec3ed6e02852278a570c9bb09a37dfaf1838971be126c0 \
  ./infra/ec2/launch-inference.sh

./infra/ec2/describe-inference.sh i-REPLACE_AFTER_LAUNCH
```

IAM changes can take a few seconds to propagate. The instance bootstrap can
take several minutes because it installs Docker and pulls the image. Success is
not merely an EC2 `running` state: console evidence must contain all four lines:

```text
DEPLOYED_REFERENCE 955519187689.dkr.ecr.ap-northeast-1.amazonaws.com/epex-forecaster@sha256:...
IMAGE_IDENTITY sha256:...
HEALTH_RESPONSE {"status":"ok","model_loaded":true,...}
PREDICTION_RESPONSE {"model_name":...}
PHASE5_OK ...
```

After that, verify the role has only the `ecr-pull` inline policy, the security
group has an empty ingress list, IMDS reports `required`, and the instance tag
contains the approved image digest.

## Cost and teardown

This phase creates billable resources: EC2 runtime, a 12 GiB gp3 root volume,
and a public IPv4 address; data transfer may also apply. IAM roles/profiles and
security groups do not themselves have hourly charges. Do not assume an AWS
free tier applies to this account or date.

The public IPv4 address is used only for outbound package/ECR access in the
default public subnet. It does not make the API reachable because the security
group permits no inbound traffic and Docker binds the service to loopback.

To stop all Phase-5 compute/storage charges after collecting evidence:

```bash
./infra/ec2/terminate-inference.sh i-EXACT_INSTANCE_ID terminate
```

The script checks the instance's `Name` tag before termination. The root volume
is configured `DeleteOnTermination=true`. Do not run teardown while the learning
endpoint is still wanted.

## Learning resources

Read these in order and map each concept back to the table above:

1. [AWS: IAM roles for Amazon EC2](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/iam-roles-for-amazon-ec2.html) — why the role and instance profile are separate CLI resources and why static keys are unnecessary.
2. [AWS: EC2 user data](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/user-data.html) — how first-boot bootstrap runs and where its evidence is logged.
3. [AWS: configure IMDS for new instances](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/configuring-IMDS-new-instances.html) — tokens, hop limits and container considerations.
4. [AWS: private registry authentication](https://docs.aws.amazon.com/AmazonECR/latest/userguide/registry_auth.html) — how the host trades role credentials for a short-lived ECR login.
5. [AWS: security groups](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-security-groups.html) — stateful network policy around the instance.

The most important operational distinction is: an instance being `running`
only proves that a VM booted. A healthy, digest-pinned prediction response is
the evidence that the ML deployment worked.
