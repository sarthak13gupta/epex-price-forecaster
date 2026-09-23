# Phase 5 — EC2 inference host in Tokyo

This phase takes the immutable image produced in Phase 4 and runs it on one
Amazon EC2 instance in `ap-northeast-1`. It is intentionally private at this
checkpoint: the instance has no inbound security-group rules and the API is
published only on host loopback. Public HTTPS, DNS and a controlled ingress
path belong to the next phase.

Last updated: **2026-09-23**.

## Status

**Complete on 2026-09-23.** The private inference host is running and the exact
Phase-4 image digest returned both a healthy model response and a real
three-day forecast. No public request path has been opened.

| Resource | Deployed value |
|---|---|
| AWS account / region | `955519187689` / `ap-northeast-1` |
| Instance | `i-0290d8f3733e43a43`, `t3.micro`, running |
| Availability Zone | `ap-northeast-1a` |
| AMI | `ami-06380d26ad7176f2c` — `al2023-ami-2023.12.20260918.0-kernel-6.18-x86_64` |
| Security group | `sg-0a663d88dbd652394`, empty ingress |
| Instance role | `epex-forecaster-ec2-role` |
| Instance profile | `epex-forecaster-ec2-profile` |
| Root volume | `vol-0df8bdf56a33a7c47`, encrypted 12 GiB gp3, delete on termination |
| Public IPv4 | `43.207.203.38` for outbound bootstrap; no inbound access |
| Container image ID | `sha256:cbc80bbc44b8b6e3d755fb3998ebeedfb19be023f79cbc33bf847ef3778eaf1e` |

The AWS timestamps in the evidence are UTC (`2026-09-22 19:24–19:26`), which
is `2026-09-23 00:54–00:56` in Asia/Kolkata.

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

## Bootstrap permission history and required cleanup

The first read-only preflight failed because the configured CLI user could not
call `ec2:DescribeVpcs`. Through a privileged AWS Console session, the account
owner attached `policy-phase5-provisioner.json` as the inline policy
`Phase5ProvisionerTemporary` to `quantitative-pipeline-user`. This was enough to
create and inspect the named IAM/EC2 resources without restoring broad
`AdministratorAccess`.

**Manual action now required:** remove `Phase5ProvisionerTemporary` from
`quantitative-pipeline-user`. It has finished its job and includes control-plane
permissions such as `ec2:RunInstances` and `ec2:TerminateInstances`. Removing
it does not affect the running instance: EC2 uses its separate instance role.

## Reproduction commands

```bash
cd /Users/sarthakgupta/GENAI/epex-price-forecaster
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
export AWS_PROFILE=admin
export AWS_DEFAULT_REGION=ap-northeast-1

./infra/iam/apply-ec2-ecr-pull.sh

IMAGE_DIGEST=sha256:2e65ee5f6ce3d26d9bec3ed6e02852278a570c9bb09a37dfaf1838971be126c0 \
  ./infra/ec2/launch-inference.sh

./infra/ec2/describe-inference.sh i-0290d8f3733e43a43
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

## Recorded acceptance evidence

The instance reached both EC2 status checks `ok`. Its AMI owner is the Amazon
Linux owner `137112412989`, architecture is `x86_64`, IMDSv2 is required, and
the security group has no ingress entries. The role inspection returned one
inline policy named `ecr-pull` and no attached managed policies.

Cloud-init recorded:

```text
health attempt 6: healthy
DEPLOYED_REFERENCE 955519187689.dkr.ecr.ap-northeast-1.amazonaws.com/epex-forecaster@sha256:2e65ee5f6ce3d26d9bec3ed6e02852278a570c9bb09a37dfaf1838971be126c0
IMAGE_IDENTITY sha256:cbc80bbc44b8b6e3d755fb3998ebeedfb19be023f79cbc33bf847ef3778eaf1e
HEALTH_RESPONSE {"status":"ok","model_loaded":true,"model_uri":"/app/model","detail":null}
PHASE5_OK 2026-09-22T19:26:49Z
```

The prediction check returned model `XGBoost`, training end `2020-06-30`, and
the expected three prices:

```text
2020-07-01  34.68523989365982
2020-07-02  35.20491425207375
2020-07-03  34.94565669127315
mean        34.94527027900224
```

This proves more than a running VM: the instance role authenticated to ECR,
Docker pulled the approved bytes, the bundled model deserialized, and the
inference cascade completed on the target Linux/amd64 host.

One final control-plane evidence query, `ecr:DescribeImages`, was denied to the
human CLI identity because the temporary provisioner policy deliberately does
not include ECR read access. This did not affect deployment: Phase 4 already
recorded the repository digest, and the EC2 console evidence records the exact
same `repository@digest` reference after a successful role-authenticated pull.

## Cost and teardown

This phase creates billable resources: EC2 runtime, a 12 GiB gp3 root volume,
and a public IPv4 address; data transfer may also apply. IAM roles/profiles and
security groups do not themselves have hourly charges. Do not assume an AWS
free tier applies to this account or date.

The public IPv4 address is used only for outbound package/ECR access in the
default public subnet. It does not make the API reachable because the security
group permits no inbound traffic and Docker binds the service to loopback.

The instance is deliberately still running for the next phase. To stop all
Phase-5 compute/storage charges when it is no longer needed:

```bash
./infra/ec2/terminate-inference.sh i-0290d8f3733e43a43 terminate
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

## Next boundary

Phase 5 proves private serving. It does not yet provide a user-facing endpoint,
TLS, DNS, load balancing, monitoring, automated redeployment or rollback. The
next deployment phase should add a deliberately controlled HTTPS request path
without exposing port 8000, SSH, MLflow or Docker directly.
