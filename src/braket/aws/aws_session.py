# Copyright Amazon.com Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You
# may not use this file except in compliance with the License. A copy of
# the License is located at
#
#     http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.

from __future__ import annotations

import itertools
import os
import os.path
import re
import warnings
from functools import cache
from pathlib import Path
from typing import Any, NamedTuple, Optional

import backoff
import boto3
import braket._schemas as braket_schemas
from botocore import awsrequest, client
from botocore.config import Config
from botocore.exceptions import ClientError

import braket._sdk as braket_sdk
from braket.tracking.tracking_context import active_trackers, broadcast_event
from braket.tracking.tracking_events import _TaskCreationEvent, _TaskStatusEvent


class AwsSession:  # noqa: PLR0904
    """Manage interactions with AWS services."""

    class S3DestinationFolder(NamedTuple):
        """A `NamedTuple` for an S3 bucket and object key."""

        bucket: str
        key: str

    def __init__(
        self,
        boto_session: Optional[boto3.Session] = None,
        braket_client: Optional[client] = None,
        config: Optional[Config] = None,
        default_bucket: Optional[str] = None,
    ):
        """Initializes an `AwsSession`.

        Args:
            boto_session (boto3.Session | None): A boto3 session object.
            braket_client (client | None): A boto3 Braket client.
            config (Config | None): A botocore Config object.
            default_bucket (str | None): The name of the default bucket of the AWS Session.

        Raises:
            ValueError: invalid boto_session or braket_client.
        """
        if (
            boto_session
            and braket_client
            and boto_session.region_name != braket_client.meta.region_name
        ):
            raise ValueError(
                "Boto Session region and Braket Client region must match and currently "
                f"they do not: Boto Session region is '{boto_session.region_name}', but "
                f"Braket Client region is '{braket_client.meta.region_name}'."
            )

        self._update_user_agent()
        self._config = Config(user_agent_extra=self._braket_user_agents)
        if config:
            self._config = self._config.merge(config)

        if braket_client:
            braket_client._client_config = (
                self._config.merge(braket_client._client_config)
                if braket_client._client_config
                else self._config
            )
            self.boto_session = boto_session or boto3.Session(
                region_name=braket_client.meta.region_name
            )
            self.braket_client = braket_client
            self._config = braket_client._client_config
        else:
            self.boto_session = boto_session or boto3.Session(
                region_name=os.environ.get("AWS_REGION")
            )
            self.braket_client = self.boto_session.client(
                "braket", config=self._config, endpoint_url=os.environ.get("BRAKET_ENDPOINT")
            )
        self._braket_user_agents = self._config._user_provided_options["user_agent_extra"]
        self._custom_default_bucket = bool(default_bucket)
        self._default_bucket = default_bucket or os.environ.get("AMZN_BRAKET_OUT_S3_BUCKET")
        self.braket_client.meta.events.register(
            "before-sign.braket.CreateQuantumTask", self._add_cost_tracker_count_handler
        )

        self._iam = None
        self._s3 = None
        self._sts = None
        self._logs = None
        self._ecr = None
        self._account_id = None

    @property
    def region(self) -> str:
        return self.boto_session.region_name

    @property
    def account_id(self) -> str:
        """Gets the caller's account number.

        Returns:
            str: The account number of the caller.
        """
        if not self._account_id:
            self._account_id = self.sts_client.get_caller_identity()["Account"]
        return self._account_id

    @property
    def iam_client(self) -> client:
        """Gets the IAM client.

        Returns:
            client: The IAM Client.
        """
        if not self._iam:
            self._iam = self.boto_session.client("iam", region_name=self.region)
        return self._iam

    @property
    def s3_client(self) -> client:
        """Gets the S3 client.

        Returns:
            client: The S3 Client.
        """
        if not self._s3:
            self._s3 = self.boto_session.client("s3", region_name=self.region)
        return self._s3

    @property
    def sts_client(self) -> client:
        """Gets the STS client.

        Returns:
            client: The STS Client.
        """
        if not self._sts:
            self._sts = self.boto_session.client("sts", region_name=self.region)
        return self._sts

    @property
    def logs_client(self) -> client:
        """Gets the CloudWatch logs client.

        Returns:
            client: The CloudWatch logs Client.
        """
        if not self._logs:
            self._logs = self.boto_session.client("logs", region_name=self.region)
        return self._logs

    @property
    def ecr_client(self) -> client:
        """Gets the ECR client.

        Returns:
            client: The ECR Client.
        """
        if not self._ecr:
            self._ecr = self.boto_session.client("ecr", region_name=self.region)
        return self._ecr

    def _update_user_agent(self) -> None:
        """Updates the `User-Agent` header forwarded by boto3 to include the braket-sdk,
        braket-schemas and the notebook instance version. The header is a string of space delimited
        values (For example: "Boto3/1.14.43 Python/3.7.9 Botocore/1.17.44").
        """

        def _notebook_instance_version() -> str:
            # TODO: Replace with lifecycle configuration version once we have a way to access those
            nbi_metadata_path = "/opt/ml/metadata/resource-metadata.json"
            return "0" if os.path.exists(nbi_metadata_path) else "None"

        self._braket_user_agents = (
            f"BraketSdk/{braket_sdk.__version__} "
            f"BraketSchemas/{braket_schemas.__version__} "
            f"NotebookInstance/{_notebook_instance_version()}"
        )

    def add_braket_user_agent(self, user_agent: str) -> None:
        """Appends the `user-agent` value to the User-Agent header, if it does not yet exist in the
        header. This method is typically only relevant for libraries integrating with the
        Amazon Braket SDK.

        Args:
            user_agent (str): The user_agent value to append to the header.
        """
        if user_agent not in self._braket_user_agents:
            self._braket_user_agents = f"{self._braket_user_agents} {user_agent}"

        new_user_agent_config = Config(user_agent_extra=self._braket_user_agents)
        updated_config = self.braket_client._client_config.merge(new_user_agent_config)
        self.braket_client = self.boto_session.client(
            "braket", config=updated_config, endpoint_url=os.environ.get("BRAKET_ENDPOINT")
        )

    @staticmethod
    def _add_cost_tracker_count_handler(request: awsrequest.AWSRequest, **kwargs) -> None:  # noqa: ARG004
        request.headers.add_header("Braket-Trackers", str(len(active_trackers())))

    #
    # Quantum Tasks
    #
    def cancel_quantum_task(self, arn: str) -> None:
        """Cancel the quantum task.

        Args:
            arn (str): The ARN of the quantum task to cancel.
        """
        response = self.braket_client.cancel_quantum_task(quantumTaskArn=arn)
        broadcast_event(_TaskStatusEvent(arn=arn, status=response["cancellationStatus"]))

    def create_quantum_task(self, **boto3_kwargs) -> str:
        """Create a quantum task.

        Args:
            **boto3_kwargs: Keyword arguments for the Amazon Braket `CreateQuantumTask`
                operation.

        Returns:
            str: The ARN of the quantum task.
        """
        # Add reservation arn if available and device is correct.
        context_device_arn = os.getenv("AMZN_BRAKET_RESERVATION_DEVICE_ARN")
        context_reservation_arn = os.getenv("AMZN_BRAKET_RESERVATION_TIME_WINDOW_ARN")

        # if the task has a reservation_arn and also context does, raise a warning
        # Raise warning if reservation ARN is found in both context and task parameters
        task_has_reservation = any(
            item.get("type") == "RESERVATION_TIME_WINDOW_ARN"
            for item in boto3_kwargs.get("associations", [])
        )
        if task_has_reservation and context_reservation_arn:
            warnings.warn(
                "A reservation ARN was passed to 'CreateQuantumTask', but it is being overridden "
                "by a 'DirectReservation' context. If this was not intended, please review your "
                "reservation ARN settings or the context in which 'CreateQuantumTask' is called.",
                stacklevel=2,
            )

        # Ensure reservation only applies to specific device
        if context_device_arn == boto3_kwargs["deviceArn"] and context_reservation_arn:
            boto3_kwargs["associations"] = [
                {
                    "arn": context_reservation_arn,
                    "type": "RESERVATION_TIME_WINDOW_ARN",
                }
            ]

        # Add job token to request, if available.
        job_token = os.getenv("AMZN_BRAKET_JOB_TOKEN")
        if job_token:
            boto3_kwargs["jobToken"] = job_token
        response = self.braket_client.create_quantum_task(**boto3_kwargs)
        broadcast_event(
            _TaskCreationEvent(
                arn=response["quantumTaskArn"],
                shots=boto3_kwargs["shots"],
                is_job_task=(job_token is not None),
                device=boto3_kwargs["deviceArn"],
            )
        )
        return response["quantumTaskArn"]

    def create_job(self, **boto3_kwargs) -> str:
        """Create a quantum hybrid job.

        Args:
            **boto3_kwargs: Keyword arguments for the Amazon Braket `CreateJob` operation.

        Returns:
            str: The ARN of the hybrid job.
        """
        response = self.braket_client.create_job(**boto3_kwargs)
        return response["jobArn"]

    @staticmethod
    def _should_giveup(err: Exception) -> bool:
        return not (
            isinstance(err, ClientError)
            and err.response["Error"]["Code"]
            in {
                "ResourceNotFoundException",
                "ThrottlingException",
            }
        )

    @backoff.on_exception(
        backoff.expo,
        ClientError,
        max_tries=3,
        jitter=backoff.full_jitter,
        giveup=_should_giveup.__func__,
    )
    def get_quantum_task(self, arn: str) -> dict[str, Any]:
        """Gets the quantum task.

        Args:
            arn (str): The ARN of the quantum task to get.

        Returns:
            dict[str, Any]: The response from the Amazon Braket `GetQuantumTask` operation.
        """
        response = self.braket_client.get_quantum_task(
            quantumTaskArn=arn, additionalAttributeNames=["QueueInfo"]
        )
        broadcast_event(_TaskStatusEvent(arn=response["quantumTaskArn"], status=response["status"]))
        return response

    def get_default_jobs_role(self) -> str:
        """This returns the role ARN for the default hybrid jobs role created in the Amazon Braket
        Console. It will pick the first role it finds with the `RoleName` prefix
        `AmazonBraketJobsExecutionRole` with a `PathPrefix` of `/service-role/`.

        Returns:
            str: The ARN for the default IAM role for jobs execution created in the Amazon
            Braket console.

        Raises:
            RuntimeError: If no roles can be found with the prefix
                `/service-role/AmazonBraketJobsExecutionRole`.
        """
        roles_paginator = self.iam_client.get_paginator("list_roles")
        for page in roles_paginator.paginate(PathPrefix="/service-role/"):
            for role in page.get("Roles", []):
                if role["RoleName"].startswith("AmazonBraketJobsExecutionRole"):
                    return role["Arn"]
        raise RuntimeError(
            "No default jobs roles found. Please create a role using the "
            "Amazon Braket console or supply a custom role."
        )

    @backoff.on_exception(
        backoff.expo,
        ClientError,
        max_tries=3,
        jitter=backoff.full_jitter,
        giveup=_should_giveup.__func__,
    )
    def get_job(self, arn: str) -> dict[str, Any]:
        """Gets the hybrid job.

        Args:
            arn (str): The ARN of the hybrid job to get.

        Returns:
            dict[str, Any]: The response from the Amazon Braket `GetQuantumJob` operation.
        """
        return self.braket_client.get_job(jobArn=arn, additionalAttributeNames=["QueueInfo"])

    def cancel_job(self, arn: str) -> dict[str, Any]:
        """Cancel the hybrid job.

        Args:
            arn (str): The ARN of the hybrid job to cancel.

        Returns:
            dict[str, Any]: The response from the Amazon Braket `CancelJob` operation.
        """
        return self.braket_client.cancel_job(jobArn=arn)

    def retrieve_s3_object_body(self, s3_bucket: str, s3_object_key: str) -> str:
        """Retrieve the S3 object body.

        Args:
            s3_bucket (str): The S3 bucket name.
            s3_object_key (str): The S3 object key within the `s3_bucket`.

        Returns:
            str: The body of the S3 object.
        """
        s3 = self.boto_session.resource("s3", config=self._config)
        obj = s3.Object(s3_bucket, s3_object_key)
        return obj.get()["Body"].read().decode("utf-8")

    def upload_to_s3(self, filename: str, s3_uri: str) -> None:
        """Upload file to S3.

        Args:
            filename (str): local file to be uploaded.
            s3_uri (str): The S3 URI where the file will be uploaded.
        """
        bucket, key = self.parse_s3_uri(s3_uri)
        self.s3_client.upload_file(filename, bucket, key)

    def upload_local_data(self, local_prefix: str, s3_prefix: str) -> None:
        """Upload local data matching a prefix to a corresponding location in S3

        Args:
            local_prefix (str): a prefix designating files to be uploaded to S3. All files
                beginning with local_prefix will be uploaded.
            s3_prefix (str): the corresponding S3 prefix that will replace the local prefix
                when the data is uploaded. This will be an S3 URI and should include the bucket
                (i.e. 's3://my-bucket/my/prefix-')

        Example:
            local_prefix = "input", s3_prefix = "s3://my-bucket/dir/input" will upload:

            - 'input.csv' to 's3://my-bucket/dir/input.csv'
            - 'input-2.csv' to 's3://my-bucket/dir/input-2.csv'
            - 'input/data.txt' to 's3://my-bucket/dir/input/data.txt'
            - 'input-dir/data.csv' to 's3://my-bucket/dir/input-dir/data.csv'
              but will not upload:
            - 'my-input.csv'
            - 'my-dir/input.csv'

            To match all files within the directory "input" and upload them into
                "s3://my-bucket/input", provide local_prefix = "input/" and
                s3_prefix = "s3://my-bucket/input/"
        """
        # support absolute paths
        if Path(local_prefix).is_absolute():
            base_dir = Path(Path(local_prefix).anchor)
            relative_prefix = str(Path(local_prefix).relative_to(base_dir))
        else:
            base_dir = Path()
            relative_prefix = local_prefix
        for file in itertools.chain(
            # files that match the prefix
            base_dir.glob(f"{relative_prefix}*"),
            # files inside of directories that match the prefix
            base_dir.glob(f"{relative_prefix}*/**/*"),
        ):
            if file.is_file():
                s3_uri = str(file.as_posix()).replace(str(Path(local_prefix).as_posix()), s3_prefix)
                self.upload_to_s3(str(file), s3_uri)

    def download_from_s3(self, s3_uri: str, filename: str) -> None:
        """Download file from S3

        Args:
            s3_uri (str): The S3 uri from where the file will be downloaded.
            filename (str): filename to save the file to.
        """
        bucket, key = self.parse_s3_uri(s3_uri)
        self.s3_client.download_file(bucket, key, filename)

    def copy_s3_object(self, source_s3_uri: str, destination_s3_uri: str) -> None:
        """Copy object from another location in s3. Does nothing if source and
        destination URIs are the same.

        Args:
            source_s3_uri (str): S3 URI pointing to the object to be copied.
            destination_s3_uri (str): S3 URI where the object will be copied to.
        """
        if source_s3_uri == destination_s3_uri:
            return

        source_bucket, source_key = self.parse_s3_uri(source_s3_uri)
        destination_bucket, destination_key = self.parse_s3_uri(destination_s3_uri)

        self.s3_client.copy(
            {
                "Bucket": source_bucket,
                "Key": source_key,
            },
            destination_bucket,
            destination_key,
        )

    def copy_s3_directory(self, source_s3_path: str, destination_s3_path: str) -> None:
        """Copy all objects from a specified directory in S3. Does nothing if source and
        destination URIs are the same. Preserves nesting structure, will not overwrite
        other files in the destination location unless they share a name with a file
        being copied.

        Args:
            source_s3_path (str): S3 URI pointing to the directory to be copied.
            destination_s3_path (str): S3 URI where the contents of the source_s3_path
                directory will be copied to.
        """
        if source_s3_path == destination_s3_path:
            return

        source_bucket, source_prefix = AwsSession.parse_s3_uri(source_s3_path)
        destination_bucket, destination_prefix = AwsSession.parse_s3_uri(destination_s3_path)

        source_keys = self.list_keys(source_bucket, source_prefix)

        for key in source_keys:
            self.s3_client.copy(
                {
                    "Bucket": source_bucket,
                    "Key": key,
                },
                destination_bucket,
                key.replace(source_prefix, destination_prefix, 1),
            )

    def list_keys(self, bucket: str, prefix: str) -> list[str]:
        """Lists keys matching prefix in bucket.

        Args:
            bucket (str): Bucket to be queried.
            prefix (str): The S3 path prefix to be matched

        Returns:
            list[str]: A list of all keys matching the prefix in
            the bucket.
        """
        list_objects = self.s3_client.list_objects_v2(
            Bucket=bucket,
            Prefix=prefix,
        )
        keys = [obj["Key"] for obj in list_objects["Contents"]]
        while list_objects["IsTruncated"]:
            list_objects = self.s3_client.list_objects_v2(
                Bucket=bucket,
                Prefix=prefix,
                ContinuationToken=list_objects["NextContinuationToken"],
            )
            keys += [obj["Key"] for obj in list_objects["Contents"]]
        return keys

    def default_bucket(self) -> str:
        """Returns the name of the default bucket of the AWS Session. In the following order
        of priority, it will return either the parameter `default_bucket` set during
        initialization of the AwsSession (if not None), the bucket being used by the
        currently running Braket Hybrid Job (if evoked inside of a Braket Hybrid Job), or a default
        value of "amazon-braket-<aws account id>-<aws session region>. Except in the case of a user-
        specified bucket name, this method will create the default bucket if it does not
        exist.

        Returns:
            str: Name of the default bucket.
        """
        if self._default_bucket:
            return self._default_bucket
        default_bucket = f"amazon-braket-{self.region}-{self.account_id}"

        self._create_s3_bucket_if_it_does_not_exist(bucket_name=default_bucket, region=self.region)

        self._default_bucket = default_bucket
        return self._default_bucket

    def _create_s3_bucket_if_it_does_not_exist(self, bucket_name: str, region: str) -> None:
        """Creates an S3 Bucket if it does not exist.
        Also swallows a few common exceptions that indicate that the bucket already exists or
        that it is being created.

        Args:
            bucket_name (str): Name of the S3 bucket to be created.
            region (str): The region in which to create the bucket.

        Raises:
            botocore.exceptions.ClientError: If S3 throws an unexpected exception during bucket
                creation.
                If the exception is due to the bucket already existing or
                already being created, no exception is raised.
        """
        try:
            if region == "us-east-1":
                # 'us-east-1' cannot be specified because it is the default region:
                # https://github.com/boto/boto3/issues/125
                self.s3_client.create_bucket(Bucket=bucket_name)
            else:
                self.s3_client.create_bucket(
                    Bucket=bucket_name, CreateBucketConfiguration={"LocationConstraint": region}
                )
            self.s3_client.put_public_access_block(
                Bucket=bucket_name,
                PublicAccessBlockConfiguration={
                    "BlockPublicAcls": True,
                    "IgnorePublicAcls": True,
                    "BlockPublicPolicy": True,
                    "RestrictPublicBuckets": True,
                },
            )
            self.s3_client.put_bucket_policy(
                Bucket=bucket_name,
                Policy=f"""{{
                    "Version": "2012-10-17",
                    "Statement": [
                        {{
                            "Effect": "Allow",
                            "Principal": {{
                                "Service": [
                                    "braket.amazonaws.com"
                                ]
                            }},
                            "Action": "s3:*",
                            "Resource": [
                                "arn:aws:s3:::{bucket_name}",
                                "arn:aws:s3:::{bucket_name}/*"
                            ]
                        }}
                    ]
                }}""",
            )
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            message = e.response["Error"]["Message"]

            if error_code == "BucketAlreadyOwnedByYou" or (
                error_code != "BucketAlreadyExists"
                and error_code == "OperationAborted"
                and "conflicting conditional operation" in message
            ):
                pass
            elif error_code == "BucketAlreadyExists":
                raise ValueError(
                    f"Provided default bucket '{bucket_name}' already exists "
                    f"for another account. Please supply alternative "
                    f"bucket name via AwsSession constructor `AwsSession()`."
                ) from None
            else:
                raise

    def get_device(self, arn: str) -> dict[str, Any]:
        """Calls the Amazon Braket `get_device` API to retrieve device metadata.

        Args:
            arn (str): The ARN of the device.

        Returns:
            dict[str, Any]: The response from the Amazon Braket `GetDevice` operation.
        """
        return self.braket_client.get_device(deviceArn=arn)

    def search_devices(
        self,
        arns: Optional[list[str]] = None,
        names: Optional[list[str]] = None,
        types: Optional[list[str]] = None,
        statuses: Optional[list[str]] = None,
        provider_names: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        """Get devices based on filters. The result is the AND of
        all the filters `arns`, `names`, `types`, `statuses`, `provider_names`.

        Args:
            arns (Optional[list[str]]): device ARN filter, default is `None`.
            names (Optional[list[str]]): device name filter, default is `None`.
            types (Optional[list[str]]): device type filter, default is `None`.
            statuses (Optional[list[str]]): device status filter, default is `None`. When `None`
                is used, RETIRED devices will not be returned. To include RETIRED devices in
                the results, use a filter that includes "RETIRED" for this parameter.
            provider_names (Optional[list[str]]): provider name list, default is `None`.

        Returns:
            list[dict[str, Any]]: The response from the Amazon Braket `SearchDevices` operation.
        """
        filters = []
        if arns:
            filters.append({"name": "deviceArn", "values": arns})
        paginator = self.braket_client.get_paginator("search_devices")
        page_iterator = paginator.paginate(filters=filters, PaginationConfig={"MaxItems": 100})
        results = []
        for page in page_iterator:
            for result in page["devices"]:
                if names and result["deviceName"] not in names:
                    continue
                if types and result["deviceType"] not in types:
                    continue
                if statuses and result["deviceStatus"] not in statuses:
                    continue
                if statuses is None and result["deviceStatus"] == "RETIRED":
                    continue
                if provider_names and result["providerName"] not in provider_names:
                    continue
                results.append(result)
        return results

    @staticmethod
    def is_s3_uri(string: str) -> bool:
        """Determines if a given string is an S3 URI.

        Args:
            string (str): the string to check.

        Returns:
            bool: Returns True if the given string is an S3 URI.
        """
        try:
            AwsSession.parse_s3_uri(string)
        except ValueError:
            return False
        return True

    @staticmethod
    def parse_s3_uri(s3_uri: str) -> tuple[str, str]:
        """Parse S3 URI to get bucket and key

        Args:
            s3_uri (str): S3 URI.

        Returns:
            tuple[str, str]: Bucket and Key tuple.

        Raises:
            ValueError: Raises a ValueError if the provided string is not
            a valid S3 URI.
        """
        try:
            # Object URL e.g. https://my-bucket.s3.us-west-2.amazonaws.com/my/key
            # S3 URI e.g. s3://my-bucket/my/key
            s3_uri_match = re.match(r"^https://([^./]+)\.[sS]3\.[^/]+/(.+)$", s3_uri) or re.match(
                r"^[sS]3://([^./]+)/(.+)$", s3_uri
            )
            if s3_uri_match is None:
                raise AssertionError  # noqa: TRY301
            bucket, key = s3_uri_match.groups()
        except (AssertionError, ValueError) as e:
            raise ValueError(f"Not a valid S3 uri: {s3_uri}") from e
        else:
            return bucket, key

    @staticmethod
    def construct_s3_uri(bucket: str, *dirs: str) -> str:
        """Create an S3 URI given a bucket and path.

        Args:
            bucket (str): S3 URI.
            *dirs (str): directories to be appended in the resulting S3 URI

        Returns:
            str: S3 URI

        Raises:
            ValueError: Raises a ValueError if the provided arguments are not
            valid to generate an S3 URI
        """
        if not dirs:
            raise ValueError(f"Not a valid S3 location: s3://{bucket}")
        return f"s3://{bucket}/{'/'.join(dirs)}"

    def describe_log_streams(
        self,
        log_group: str,
        log_stream_prefix: str,
        limit: Optional[int] = None,
        next_token: Optional[str] = None,
    ) -> dict[str, Any]:
        """Describes CloudWatch log streams in a log group with a given prefix.

        Args:
            log_group (str): Name of the log group.
            log_stream_prefix (str): Prefix for log streams to include.
            limit (Optional[int]): Limit for number of log streams returned.
                default is 50.
            next_token (Optional[str]): The token for the next set of items to return.
                Would have been received in a previous call.

        Returns:
            dict[str, Any]: Dictionary containing logStreams and nextToken
        """
        log_stream_args = {
            "logGroupName": log_group,
            "logStreamNamePrefix": log_stream_prefix,
            "orderBy": "LogStreamName",
        }

        if limit:
            log_stream_args["limit"] = limit

        if next_token:
            log_stream_args["nextToken"] = next_token

        return self.logs_client.describe_log_streams(**log_stream_args)

    def get_log_events(
        self,
        log_group: str,
        log_stream: str,
        start_time: int,
        start_from_head: bool = True,
        next_token: Optional[str] = None,
    ) -> dict[str, Any]:
        """Gets CloudWatch log events from a given log stream.

        Args:
            log_group (str): Name of the log group.
            log_stream (str): Name of the log stream.
            start_time (int): Timestamp that indicates a start time to include log events.
            start_from_head (bool): Bool indicating to return oldest events first. default
                is True.
            next_token (Optional[str]): The token for the next set of items to return.
                Would have been received in a previous call.

        Returns:
            dict[str, Any]: Dictionary containing events, nextForwardToken, and nextBackwardToken
        """
        log_events_args = {
            "logGroupName": log_group,
            "logStreamName": log_stream,
            "startTime": start_time,
            "startFromHead": start_from_head,
        }

        if next_token:
            log_events_args["nextToken"] = next_token

        return self.logs_client.get_log_events(**log_events_args)

    def copy_session(
        self,
        region: Optional[str] = None,
        max_connections: Optional[int] = None,
    ) -> AwsSession:
        """Creates a new AwsSession based on the region.

        Args:
            region (Optional[str]): Name of the region. Default = `None`.
            max_connections (Optional[int]): The maximum number of connections in the
                Boto3 connection pool. Default = `None`.

        Returns:
            AwsSession: based on the region and boto config parameters.
        """
        config = Config(user_agent_extra=self._braket_user_agents)
        if max_connections:
            config = config.merge(Config(max_pool_connections=max_connections))

        session_region = self.boto_session.region_name
        new_region = region or session_region

        # note that this method does not copy a custom Braket endpoint URL, since those are
        # region-specific. If you have an endpoint that you wish to be used by copied AwsSessions
        # (i.e. for task batching), please use the `BRAKET_ENDPOINT` environment variable.

        creds = self.boto_session.get_credentials()
        default_bucket = self._default_bucket if self._custom_default_bucket else None
        profile_name = self.boto_session.profile_name
        profile_name = profile_name if profile_name != "default" else None
        if creds.method == "explicit":
            boto_session = boto3.Session(
                aws_access_key_id=creds.access_key,
                aws_secret_access_key=creds.secret_key,
                aws_session_token=creds.token,
                region_name=new_region,
                profile_name=profile_name,
            )
        elif creds.method == "env":
            boto_session = boto3.Session(region_name=new_region)
        else:
            boto_session = boto3.Session(
                region_name=new_region,
                profile_name=profile_name,
            )
        return AwsSession(boto_session=boto_session, config=config, default_bucket=default_bucket)

    @cache  # noqa: B019
    def get_full_image_tag(self, image_uri: str) -> str:
        """Get verbose image tag from image uri.

        Args:
            image_uri (str): Image uri to get tag for.

        Returns:
            str: Verbose image tag for given image.
        """
        registry = image_uri.split(".")[0]
        repository, tag = image_uri.split("/")[-1].split(":")

        # get image digest of latest image
        digest = self.ecr_client.batch_get_image(
            registryId=registry,
            repositoryName=repository,
            imageIds=[{"imageTag": tag}],
        )["images"][0]["imageId"]["imageDigest"]

        # get all images matching digest (same image, different tags)
        images = self.ecr_client.batch_get_image(
            registryId=registry,
            repositoryName=repository,
            imageIds=[{"imageDigest": digest}],
        )["images"]

        # find the tag with the python version info
        for image in images:
            if re.search(r"py\d\d+", tag := image["imageId"]["imageTag"]):
                return tag

        raise ValueError("Full image tag missing.")
