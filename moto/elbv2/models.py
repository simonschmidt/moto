import datetime
import re
from jinja2 import Template
from botocore.exceptions import ParamValidationError
from collections import OrderedDict
from moto.core.exceptions import RESTError
from moto.core import ACCOUNT_ID, BaseBackend, BaseModel, CloudFormationModel
from moto.core.utils import (
    iso_8601_datetime_with_milliseconds,
    get_random_hex,
    BackendDict,
)
from moto.ec2.models import ec2_backends
from moto.acm.models import acm_backends
from .utils import make_arn_for_target_group
from .utils import make_arn_for_load_balancer
from .exceptions import (
    DuplicateLoadBalancerName,
    DuplicateListenerError,
    DuplicateTargetGroupName,
    InvalidTargetError,
    ListenerNotFoundError,
    LoadBalancerNotFoundError,
    SubnetNotFoundError,
    TargetGroupNotFoundError,
    TooManyTagsError,
    PriorityInUseError,
    InvalidConditionFieldError,
    InvalidConditionValueError,
    InvalidActionTypeError,
    ActionTargetGroupNotFoundError,
    InvalidDescribeRulesRequest,
    ResourceInUseError,
    RuleNotFoundError,
    DuplicatePriorityError,
    InvalidTargetGroupNameError,
    InvalidModifyRuleArgumentsError,
    InvalidStatusCodeActionTypeError,
    InvalidLoadBalancerActionException,
)


class FakeHealthStatus(BaseModel):
    def __init__(
        self, instance_id, port, health_port, status, reason=None, description=None
    ):
        self.instance_id = instance_id
        self.port = port
        self.health_port = health_port
        self.status = status
        self.reason = reason
        self.description = description


class FakeTargetGroup(CloudFormationModel):
    HTTP_CODE_REGEX = re.compile(r"(?:(?:\d+-\d+|\d+),?)+")

    def __init__(
        self,
        name,
        arn,
        vpc_id,
        protocol,
        port,
        protocol_version=None,
        healthcheck_protocol=None,
        healthcheck_port=None,
        healthcheck_path=None,
        healthcheck_interval_seconds=None,
        healthcheck_timeout_seconds=None,
        healthcheck_enabled=None,
        healthy_threshold_count=None,
        unhealthy_threshold_count=None,
        matcher=None,
        target_type=None,
    ):

        # TODO: default values differs when you add Network Load balancer
        self.name = name
        self.arn = arn
        self.vpc_id = vpc_id
        self.protocol = protocol
        self.protocol_version = protocol_version or "HTTP1"
        self.port = port
        self.healthcheck_protocol = healthcheck_protocol or self.protocol
        self.healthcheck_port = healthcheck_port
        self.healthcheck_path = healthcheck_path
        self.healthcheck_interval_seconds = healthcheck_interval_seconds or 30
        self.healthcheck_timeout_seconds = healthcheck_timeout_seconds or 5
        self.healthcheck_enabled = healthcheck_enabled
        self.healthy_threshold_count = healthy_threshold_count or 5
        self.unhealthy_threshold_count = unhealthy_threshold_count or 2
        self.load_balancer_arns = []
        self.tags = {}
        self.matcher = matcher
        if self.protocol != "TCP":
            self.matcher = self.matcher or {"HttpCode": "200"}
            self.healthcheck_path = self.healthcheck_path or "/"
            self.healthcheck_port = self.healthcheck_port or str(self.port)
        self.target_type = target_type

        self.attributes = {
            "deregistration_delay.timeout_seconds": 300,
            "stickiness.enabled": "false",
        }

        self.targets = OrderedDict()

    @property
    def physical_resource_id(self):
        return self.arn

    def register(self, targets):
        for target in targets:
            self.targets[target["id"]] = {
                "id": target["id"],
                "port": target.get("port", self.port),
            }

    def deregister(self, targets):
        for target in targets:
            t = self.targets.pop(target["id"], None)
            if not t:
                raise InvalidTargetError()

    def deregister_terminated_instances(self, instance_ids):
        for target_id in list(self.targets.keys()):
            if target_id in instance_ids:
                del self.targets[target_id]

    def add_tag(self, key, value):
        if len(self.tags) >= 10 and key not in self.tags:
            raise TooManyTagsError()
        self.tags[key] = value

    def health_for(self, target, ec2_backend):
        t = self.targets.get(target["id"])
        if t is None:
            raise InvalidTargetError()
        if t["id"].startswith("i-"):  # EC2 instance ID
            instance = ec2_backend.get_instance_by_id(t["id"])
            if instance.state == "stopped":
                return FakeHealthStatus(
                    t["id"],
                    t["port"],
                    self.healthcheck_port,
                    "unused",
                    "Target.InvalidState",
                    "Target is in the stopped state",
                )
        return FakeHealthStatus(t["id"], t["port"], self.healthcheck_port, "healthy")

    @staticmethod
    def cloudformation_name_type():
        return "Name"

    @staticmethod
    def cloudformation_type():
        # https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-resource-elasticloadbalancingv2-targetgroup.html
        return "AWS::ElasticLoadBalancingV2::TargetGroup"

    @classmethod
    def create_from_cloudformation_json(
        cls, resource_name, cloudformation_json, region_name, **kwargs
    ):
        properties = cloudformation_json["Properties"]

        elbv2_backend = elbv2_backends[region_name]

        vpc_id = properties.get("VpcId")
        protocol = properties.get("Protocol")
        port = properties.get("Port")
        healthcheck_protocol = properties.get("HealthCheckProtocol")
        healthcheck_port = properties.get("HealthCheckPort")
        healthcheck_path = properties.get("HealthCheckPath")
        healthcheck_interval_seconds = properties.get("HealthCheckIntervalSeconds")
        healthcheck_timeout_seconds = properties.get("HealthCheckTimeoutSeconds")
        healthy_threshold_count = properties.get("HealthyThresholdCount")
        unhealthy_threshold_count = properties.get("UnhealthyThresholdCount")
        matcher = properties.get("Matcher")
        target_type = properties.get("TargetType")

        target_group = elbv2_backend.create_target_group(
            name=resource_name,
            vpc_id=vpc_id,
            protocol=protocol,
            port=port,
            healthcheck_protocol=healthcheck_protocol,
            healthcheck_port=healthcheck_port,
            healthcheck_path=healthcheck_path,
            healthcheck_interval_seconds=healthcheck_interval_seconds,
            healthcheck_timeout_seconds=healthcheck_timeout_seconds,
            healthy_threshold_count=healthy_threshold_count,
            unhealthy_threshold_count=unhealthy_threshold_count,
            matcher=matcher,
            target_type=target_type,
        )
        return target_group


class FakeListener(CloudFormationModel):
    def __init__(
        self,
        load_balancer_arn,
        arn,
        protocol,
        port,
        ssl_policy,
        certificate,
        default_actions,
    ):
        self.load_balancer_arn = load_balancer_arn
        self.arn = arn
        self.protocol = (protocol or "").upper()
        self.port = port
        self.ssl_policy = ssl_policy
        self.certificate = certificate
        self.certificates = [certificate] if certificate is not None else []
        self.default_actions = default_actions
        self._non_default_rules = OrderedDict()
        self._default_rule = OrderedDict()
        self._default_rule[0] = FakeRule(
            listener_arn=self.arn,
            conditions=[],
            priority="default",
            actions=default_actions,
            is_default=True,
        )
        self.tags = {}

    @property
    def physical_resource_id(self):
        return self.arn

    @property
    def rules(self):
        return OrderedDict(
            list(self._non_default_rules.items()) + list(self._default_rule.items())
        )

    def remove_rule(self, arn):
        self._non_default_rules.pop(arn)

    def register(self, arn, rule):
        self._non_default_rules[arn] = rule
        sorted(self._non_default_rules.values(), key=lambda x: x.priority)

    @staticmethod
    def cloudformation_name_type():
        return None

    @staticmethod
    def cloudformation_type():
        # https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-resource-elasticloadbalancingv2-listener.html
        return "AWS::ElasticLoadBalancingV2::Listener"

    @classmethod
    def create_from_cloudformation_json(
        cls, resource_name, cloudformation_json, region_name, **kwargs
    ):
        properties = cloudformation_json["Properties"]

        elbv2_backend = elbv2_backends[region_name]
        load_balancer_arn = properties.get("LoadBalancerArn")
        protocol = properties.get("Protocol")
        port = properties.get("Port")
        ssl_policy = properties.get("SslPolicy")
        certificates = properties.get("Certificates")

        default_actions = elbv2_backend.convert_and_validate_properties(properties)
        certificates = elbv2_backend.convert_and_validate_certificates(certificates)
        listener = elbv2_backend.create_listener(
            load_balancer_arn, protocol, port, ssl_policy, certificates, default_actions
        )
        return listener

    @classmethod
    def update_from_cloudformation_json(
        cls, original_resource, new_resource_name, cloudformation_json, region_name
    ):
        properties = cloudformation_json["Properties"]

        elbv2_backend = elbv2_backends[region_name]
        protocol = properties.get("Protocol")
        port = properties.get("Port")
        ssl_policy = properties.get("SslPolicy")
        certificates = properties.get("Certificates")

        default_actions = elbv2_backend.convert_and_validate_properties(properties)
        certificates = elbv2_backend.convert_and_validate_certificates(certificates)
        listener = elbv2_backend.modify_listener(
            original_resource.arn,
            port,
            protocol,
            ssl_policy,
            certificates,
            default_actions,
        )
        return listener


class FakeListenerRule(CloudFormationModel):
    def __init__(
        self, listener_arn, arn, conditions, priority, actions,
    ):
        self.listener_arn = listener_arn
        self.arn = arn
        self.conditions = conditions
        self.actions = actions
        self.priority = priority

    @property
    def physical_resource_id(self):
        return self.arn

    @staticmethod
    def cloudformation_type():
        # https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-resource-elasticloadbalancingv2-listenerrule.html
        return "AWS::ElasticLoadBalancingV2::ListenerRule"

    @classmethod
    def create_from_cloudformation_json(
        cls, resource_name, cloudformation_json, region_name, **kwargs
    ):
        properties = cloudformation_json["Properties"]
        elbv2_backend = elbv2_backends[region_name]
        listener_arn = properties.get("ListenerArn")
        priority = properties.get("Priority")
        conditions = properties.get("Conditions")

        actions = elbv2_backend.convert_and_validate_action_properties(properties)
        listener_rule = elbv2_backend.create_rule(
            listener_arn, conditions, priority, actions
        )
        return listener_rule

    @classmethod
    def update_from_cloudformation_json(
        cls, original_resource, new_resource_name, cloudformation_json, region_name
    ):

        properties = cloudformation_json["Properties"]

        elbv2_backend = elbv2_backends[region_name]
        conditions = properties.get("Conditions")

        actions = elbv2_backend.convert_and_validate_action_properties(properties)
        listener_rule = elbv2_backend.modify_rule(
            original_resource.arn, conditions, actions
        )
        return listener_rule


class FakeRule(BaseModel):
    def __init__(self, listener_arn, conditions, priority, actions, is_default):
        self.listener_arn = listener_arn
        self.arn = listener_arn.replace(":listener/", ":listener-rule/") + "/%s" % (
            id(self)
        )
        self.conditions = conditions
        self.priority = priority  # int or 'default'
        self.actions = actions
        self.is_default = is_default


class FakeAction(BaseModel):
    def __init__(self, data):
        self.data = data
        self.type = data.get("Type")

    def to_xml(self):
        template = Template(
            """<Type>{{ action.type }}</Type>
            {% if action.type == "forward" and "ForwardConfig" in action.data %}
            <ForwardConfig>
              <TargetGroups>
                {% for target_group in action.data["ForwardConfig"]["TargetGroups"] %}
                <member>
                  <TargetGroupArn>{{ target_group["TargetGroupArn"] }}</TargetGroupArn>
                  <Weight>{{ target_group["Weight"] }}</Weight>
                </member>
                {% endfor %}
              </TargetGroups>
            </ForwardConfig>
            {% endif %}
            {% if action.type == "forward" and "ForwardConfig" not in action.data %}
            <TargetGroupArn>{{ action.data["TargetGroupArn"] }}</TargetGroupArn>
            {% elif action.type == "redirect" %}
            <RedirectConfig>
                <Protocol>{{ action.data["RedirectConfig"]["Protocol"] }}</Protocol>
                <Port>{{ action.data["RedirectConfig"]["Port"] }}</Port>
                <StatusCode>{{ action.data["RedirectConfig"]["StatusCode"] }}</StatusCode>
            </RedirectConfig>
            {% elif action.type == "authenticate-cognito" %}
            <AuthenticateCognitoConfig>
                <UserPoolArn>{{ action.data["AuthenticateCognitoConfig"]["UserPoolArn"] }}</UserPoolArn>
                <UserPoolClientId>{{ action.data["AuthenticateCognitoConfig"]["UserPoolClientId"] }}</UserPoolClientId>
                <UserPoolDomain>{{ action.data["AuthenticateCognitoConfig"]["UserPoolDomain"] }}</UserPoolDomain>
            </AuthenticateCognitoConfig>
            {% elif action.type == "fixed-response" %}
             <FixedResponseConfig>
                <ContentType>{{ action.data["FixedResponseConfig"]["ContentType"] }}</ContentType>
                <MessageBody>{{ action.data["FixedResponseConfig"]["MessageBody"] }}</MessageBody>
                <StatusCode>{{ action.data["FixedResponseConfig"]["StatusCode"] }}</StatusCode>
            </FixedResponseConfig>
            {% endif %}
            """
        )
        return template.render(action=self)


class FakeBackend(BaseModel):
    def __init__(self, instance_port):
        self.instance_port = instance_port
        self.policy_names = []

    def __repr__(self):
        return "FakeBackend(inp: %s, policies: %s)" % (
            self.instance_port,
            self.policy_names,
        )


class FakeLoadBalancer(CloudFormationModel):
    VALID_ATTRS = {
        "access_logs.s3.enabled",
        "access_logs.s3.bucket",
        "access_logs.s3.prefix",
        "deletion_protection.enabled",
        "idle_timeout.timeout_seconds",
        "load_balancing.cross_zone.enabled",
        "routing.http2.enabled",
        "routing.http.drop_invalid_header_fields.enabled",
    }

    def __init__(
        self,
        name,
        security_groups,
        subnets,
        vpc_id,
        arn,
        dns_name,
        state,
        scheme="internet-facing",
        loadbalancer_type=None,
    ):
        self.name = name
        self.created_time = iso_8601_datetime_with_milliseconds(datetime.datetime.now())
        self.scheme = scheme
        self.security_groups = security_groups
        self.subnets = subnets or []
        self.vpc_id = vpc_id
        self.listeners = OrderedDict()
        self.tags = {}
        self.arn = arn
        self.dns_name = dns_name
        self.state = state
        self.loadbalancer_type = loadbalancer_type or "application"

        self.stack = "ipv4"
        self.attrs = {
            # "access_logs.s3.enabled": "false",  # commented out for TF compatibility
            "access_logs.s3.bucket": None,
            "access_logs.s3.prefix": None,
            "deletion_protection.enabled": "false",
            # "idle_timeout.timeout_seconds": "60",  # commented out for TF compatibility
        }

    @property
    def physical_resource_id(self):
        return self.arn

    def add_tag(self, key, value):
        if len(self.tags) >= 10 and key not in self.tags:
            raise TooManyTagsError()
        self.tags[key] = value

    def list_tags(self):
        return self.tags

    def remove_tag(self, key):
        if key in self.tags:
            del self.tags[key]

    def activate(self):
        if self.state == "provisioning":
            self.state = "active"

    def delete(self, region):
        """Not exposed as part of the ELB API - used for CloudFormation."""
        elbv2_backends[region].delete_load_balancer(self.arn)

    @staticmethod
    def cloudformation_name_type():
        return "Name"

    @staticmethod
    def cloudformation_type():
        # https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-resource-elasticloadbalancingv2-loadbalancer.html
        return "AWS::ElasticLoadBalancingV2::LoadBalancer"

    @classmethod
    def create_from_cloudformation_json(
        cls, resource_name, cloudformation_json, region_name, **kwargs
    ):
        properties = cloudformation_json["Properties"]

        elbv2_backend = elbv2_backends[region_name]

        security_groups = properties.get("SecurityGroups")
        subnet_ids = properties.get("Subnets")
        scheme = properties.get("Scheme", "internet-facing")

        load_balancer = elbv2_backend.create_load_balancer(
            resource_name, security_groups, subnet_ids, scheme=scheme
        )
        return load_balancer

    @classmethod
    def has_cfn_attr(cls, attribute):
        return attribute in [
            "DNSName",
            "LoadBalancerName",
            "CanonicalHostedZoneID",
            "LoadBalancerFullName",
            "SecurityGroups",
        ]

    def get_cfn_attribute(self, attribute_name):
        """
        Implemented attributes:
        * DNSName
        * LoadBalancerName

        Not implemented:
        * CanonicalHostedZoneID
        * LoadBalancerFullName
        * SecurityGroups

        This method is similar to models.py:FakeLoadBalancer.get_cfn_attribute()
        """
        from moto.cloudformation.exceptions import UnformattedGetAttTemplateException

        not_implemented_yet = [
            "CanonicalHostedZoneID",
            "LoadBalancerFullName",
            "SecurityGroups",
        ]
        if attribute_name == "DNSName":
            return self.dns_name
        elif attribute_name == "LoadBalancerName":
            return self.name
        elif attribute_name in not_implemented_yet:
            raise NotImplementedError(
                '"Fn::GetAtt" : [ "{0}" , "%s" ]"' % attribute_name
            )
        else:
            raise UnformattedGetAttTemplateException()


class ELBv2Backend(BaseBackend):
    def __init__(self, region_name=None):
        self.region_name = region_name
        self.target_groups = OrderedDict()
        self.load_balancers = OrderedDict()

    @staticmethod
    def default_vpc_endpoint_service(service_region, zones):
        """Default VPC endpoint service."""
        return BaseBackend.default_vpc_endpoint_service_factory(
            service_region, zones, "elasticloadbalancing"
        )

    @property
    def ec2_backend(self):
        """
        EC2 backend

        :return: EC2 Backend
        :rtype: moto.ec2.models.EC2Backend
        """
        return ec2_backends[self.region_name]

    @property
    def acm_backend(self):
        """
        ACM backend

        :return: ACM Backend
        :rtype: moto.acm.models.AWSCertificateManagerBackend
        """
        return acm_backends[self.region_name]

    def reset(self):
        region_name = self.region_name
        self.__dict__ = {}
        self.__init__(region_name)

    def create_load_balancer(
        self,
        name,
        security_groups,
        subnet_ids,
        scheme="internet-facing",
        loadbalancer_type=None,
    ):
        vpc_id = None
        subnets = []
        state = "provisioning"

        if not subnet_ids:
            raise SubnetNotFoundError()
        for subnet_id in subnet_ids:
            subnet = self.ec2_backend.get_subnet(subnet_id)
            if subnet is None:
                raise SubnetNotFoundError()
            subnets.append(subnet)

        vpc_id = subnets[0].vpc_id
        arn = make_arn_for_load_balancer(
            account_id=ACCOUNT_ID, name=name, region_name=self.region_name
        )
        dns_name = "%s-1.%s.elb.amazonaws.com" % (name, self.region_name)

        if arn in self.load_balancers:
            raise DuplicateLoadBalancerName()

        new_load_balancer = FakeLoadBalancer(
            name=name,
            security_groups=security_groups,
            arn=arn,
            scheme=scheme,
            subnets=subnets,
            vpc_id=vpc_id,
            dns_name=dns_name,
            state=state,
            loadbalancer_type=loadbalancer_type,
        )
        self.load_balancers[arn] = new_load_balancer
        return new_load_balancer

    def convert_and_validate_action_properties(self, properties):

        # transform Actions to confirm with the rest of the code and XML templates
        default_actions = []
        for i, action in enumerate(properties["Actions"]):
            action_type = action["Type"]
            if action_type in [
                "redirect",
                "authenticate-cognito",
                "fixed-response",
                "forward",
            ]:
                default_actions.append(action)
            else:
                raise InvalidActionTypeError(action_type, i + 1)
        return default_actions

    def create_rule(self, listener_arn, conditions, priority, actions):
        actions = [FakeAction(action) for action in actions]
        listeners = self.describe_listeners(None, [listener_arn])
        if not listeners:
            raise ListenerNotFoundError()
        listener = listeners[0]

        # validate conditions
        # see: https://docs.aws.amazon.com/cli/latest/reference/elbv2/create-rule.html
        self._validate_conditions(conditions)

        # TODO: check QueryStringConfig condition
        # TODO: check HttpRequestMethodConfig condition
        # TODO: check SourceIpConfig condition
        # TODO: check pattern of value for 'host-header'
        # TODO: check pattern of value for 'path-pattern'

        # validate Priority
        for rule in listener.rules.values():
            if rule.priority == priority:
                raise PriorityInUseError()

        self._validate_actions(actions)
        arn = listener_arn.replace(":listener/", ":listener-rule/")
        arn += "/%s" % (get_random_hex(16))

        # TODO: check for error 'TooManyRegistrationsForTargetId'
        # TODO: check for error 'TooManyRules'

        # create rule
        rule = FakeListenerRule(listener.arn, arn, conditions, priority, actions,)
        listener.register(arn, rule)
        return rule

    def _validate_conditions(self, conditions):
        for condition in conditions:
            if "Field" in condition:
                field = condition["Field"]
                if field not in [
                    "host-header",
                    "http-header",
                    "http-request-method",
                    "path-pattern",
                    "query-string",
                    "source-ip",
                ]:
                    raise InvalidConditionFieldError(field)
                if "Values" in condition and field not in [
                    "host-header",
                    "path-pattern",
                ]:
                    raise InvalidConditionValueError(
                        "The 'Values' field is not compatible with '%s'" % field
                    )
                else:
                    method_name = "_validate_" + field.replace("-", "_") + "_condition"
                    func = getattr(self, method_name)
                    func(condition)

    def _validate_host_header_condition(self, condition):
        values = None
        if "HostHeaderConfig" in condition:
            values = condition["HostHeaderConfig"]["Values"]
        elif "Values" in condition:
            values = condition["Values"]
            if len(values) > 1:
                raise InvalidConditionValueError(
                    "The 'host-header' field contains too many values; the limit is '1'"
                )
        if values is None or len(values) == 0:
            raise InvalidConditionValueError("A condition value must be specified")
        for value in values:
            if len(value) > 128:
                raise InvalidConditionValueError(
                    "The 'host-header' value is too long; the limit is '128'"
                )

    def _validate_http_header_condition(self, condition):
        if "HttpHeaderConfig" in condition:
            config = condition["HttpHeaderConfig"]
            name = config.get("HttpHeaderName")
            if len(name) > 40:
                raise InvalidConditionValueError(
                    "The 'HttpHeaderName' value is too long; the limit is '40'"
                )
            values = config["Values"]
            for value in values:
                if len(value) > 128:
                    raise InvalidConditionValueError(
                        "The 'http-header' value is too long; the limit is '128'"
                    )
        else:
            raise InvalidConditionValueError(
                "A 'HttpHeaderConfig' must be specified with 'http-header'"
            )

    def _validate_http_request_method_condition(self, condition):
        if "HttpRequestMethodConfig" in condition:
            for value in condition["HttpRequestMethodConfig"]["Values"]:
                if len(value) > 40:
                    raise InvalidConditionValueError(
                        "The 'http-request-method' value is too long; the limit is '40'"
                    )
                if not re.match("[A-Z_-]+", value):
                    raise InvalidConditionValueError(
                        "The 'http-request-method' value is invalid; the allowed characters are A-Z, hyphen and underscore"
                    )
        else:
            raise InvalidConditionValueError(
                "A 'HttpRequestMethodConfig' must be specified with 'http-request-method'"
            )

    def _validate_path_pattern_condition(self, condition):
        values = None
        if "PathPatternConfig" in condition:
            values = condition["PathPatternConfig"]["Values"]
        elif "Values" in condition:
            values = condition["Values"]
            if len(values) > 1:
                raise InvalidConditionValueError(
                    "The 'path-pattern' field contains too many values; the limit is '1'"
                )
        if values is None or len(values) == 0:
            raise InvalidConditionValueError("A condition value must be specified")
        if condition.get("Values") and condition.get("PathPatternConfig"):
            raise InvalidConditionValueError(
                "You cannot provide both Values and 'PathPatternConfig' for a condition of type 'path-pattern'"
            )
        for value in values:
            if len(value) > 128:
                raise InvalidConditionValueError(
                    "The 'path-pattern' value is too long; the limit is '128'"
                )

    def _validate_source_ip_condition(self, condition):
        if "SourceIpConfig" in condition:
            values = condition["SourceIpConfig"].get("Values", [])
            if len(values) == 0:
                raise InvalidConditionValueError(
                    "A 'source-ip' value must be specified"
                )
        else:
            raise InvalidConditionValueError(
                "A 'SourceIpConfig' must be specified with 'source-ip'"
            )

    def _validate_query_string_condition(self, condition):
        if "QueryStringConfig" in condition:
            config = condition["QueryStringConfig"]
            values = config["Values"]
            for value in values:
                if "Value" not in value:
                    raise InvalidConditionValueError(
                        "A 'Value' must be specified in 'QueryStringKeyValuePair'"
                    )
                if "Key" in value and len(value["Key"]) > 128:
                    raise InvalidConditionValueError(
                        "The 'Key' value is too long; the limit is '128'"
                    )
                if len(value["Value"]) > 128:
                    raise InvalidConditionValueError(
                        "The 'Value' value is too long; the limit is '128'"
                    )
        else:
            raise InvalidConditionValueError(
                "A 'QueryStringConfig' must be specified with 'query-string'"
            )

    def _get_target_group_arns_from(self, action_data):
        if "TargetGroupArn" in action_data:
            return [action_data["TargetGroupArn"]]
        elif "ForwardConfig" in action_data:
            return [
                tg["TargetGroupArn"]
                for tg in action_data["ForwardConfig"].get("TargetGroups", [])
            ]
        else:
            return []

    def _validate_actions(self, actions):
        # validate Actions
        target_group_arns = [
            target_group.arn for target_group in self.target_groups.values()
        ]
        for i, action in enumerate(actions):
            index = i + 1
            action_type = action.type
            if action_type == "forward":
                found_arns = self._get_target_group_arns_from(action_data=action.data)
                for target_group_arn in found_arns:
                    if target_group_arn not in target_group_arns:
                        raise ActionTargetGroupNotFoundError(target_group_arn)
            elif action_type == "fixed-response":
                self._validate_fixed_response_action(action, i, index)
            elif action_type in ["redirect", "authenticate-cognito"]:
                pass
            # pass if listener rule has forward_config as an Action property
            elif action_type == "forward" and "ForwardConfig" in action.data.keys():
                pass
            else:
                raise InvalidActionTypeError(action_type, index)

    def _validate_fixed_response_action(self, action, i, index):
        status_code = action.data.get("FixedResponseConfig", {}).get("StatusCode")
        if status_code is None:
            raise ParamValidationError(
                report='Missing required parameter in Actions[%s].FixedResponseConfig: "StatusCode"'
                % i
            )
        expression = r"^(2|4|5)\d\d$"
        if not re.match(expression, status_code):
            raise InvalidStatusCodeActionTypeError(
                "1 validation error detected: Value '{}' at 'actions.{}.member.fixedResponseConfig.statusCode' failed to satisfy constraint: \
Member must satisfy regular expression pattern: {}".format(
                    status_code, index, expression
                )
            )
        content_type = action.data["FixedResponseConfig"].get("ContentType")
        if content_type and content_type not in [
            "text/plain",
            "text/css",
            "text/html",
            "application/javascript",
            "application/json",
        ]:
            raise InvalidLoadBalancerActionException(
                "The ContentType must be one of:'text/html', 'application/json', 'application/javascript', 'text/css', 'text/plain'"
            )

    def create_target_group(self, name, **kwargs):
        if len(name) > 32:
            raise InvalidTargetGroupNameError(
                "Target group name '{}' cannot be longer than '32' characters".format(
                    name
                )
            )
        if not re.match(r"^[a-zA-Z0-9\-]+$", name):
            raise InvalidTargetGroupNameError(
                "Target group name '{}' can only contain characters that are alphanumeric characters or hyphens(-)".format(
                    name
                )
            )

        # undocumented validation
        if not re.match(r"(?!.*--)(?!^-)(?!.*-$)^[A-Za-z0-9-]+$", name):
            raise InvalidTargetGroupNameError(
                "1 validation error detected: Value '%s' at 'targetGroup.targetGroupArn.targetGroupName' failed to satisfy constraint: Member must satisfy regular expression pattern: (?!.*--)(?!^-)(?!.*-$)^[A-Za-z0-9-]+$"
                % name
            )

        if name.startswith("-") or name.endswith("-"):
            raise InvalidTargetGroupNameError(
                "Target group name '%s' cannot begin or end with '-'" % name
            )
        for target_group in self.target_groups.values():
            if target_group.name == name:
                raise DuplicateTargetGroupName()

        valid_protocols = ["HTTPS", "HTTP", "TCP", "TLS", "UDP", "TCP_UDP", "GENEVE"]
        if (
            kwargs.get("healthcheck_protocol")
            and kwargs["healthcheck_protocol"] not in valid_protocols
        ):
            raise InvalidConditionValueError(
                "Value {} at 'healthCheckProtocol' failed to satisfy constraint: "
                "Member must satisfy enum value set: {}".format(
                    kwargs["healthcheck_protocol"], valid_protocols
                )
            )
        if kwargs.get("protocol") and kwargs["protocol"] not in valid_protocols:
            raise InvalidConditionValueError(
                "Value {} at 'protocol' failed to satisfy constraint: "
                "Member must satisfy enum value set: {}".format(
                    kwargs["protocol"], valid_protocols
                )
            )

        if (
            kwargs.get("matcher")
            and FakeTargetGroup.HTTP_CODE_REGEX.match(kwargs["matcher"]["HttpCode"])
            is None
        ):
            raise RESTError(
                "InvalidParameterValue",
                "HttpCode must be like 200 | 200-399 | 200,201 ...",
            )

        arn = make_arn_for_target_group(
            account_id=ACCOUNT_ID, name=name, region_name=self.region_name
        )
        target_group = FakeTargetGroup(name, arn, **kwargs)
        self.target_groups[target_group.arn] = target_group
        return target_group

    def modify_target_group_attributes(self, target_group_arn, attributes):
        target_group = self.target_groups.get(target_group_arn)
        if not target_group:
            raise TargetGroupNotFoundError()

        target_group.attributes.update(attributes)

    def convert_and_validate_certificates(self, certificates):

        # transform default certificate to conform with the rest of the code and XML templates
        for cert in certificates or []:
            cert["certificate_arn"] = cert["CertificateArn"]

        return certificates

    def convert_and_validate_properties(self, properties):

        # transform default actions to confirm with the rest of the code and XML templates
        # Caller: CF create/update for type "AWS::ElasticLoadBalancingV2::Listener"
        default_actions = []
        for i, action in enumerate(properties["DefaultActions"]):
            action_type = action["Type"]
            if action_type == "forward":
                default_actions.append(
                    {"Type": action_type, "TargetGroupArn": action["TargetGroupArn"]}
                )
            elif action_type in [
                "redirect",
                "authenticate-cognito",
                "fixed-response",
            ]:
                default_actions.append(action)
            else:
                raise InvalidActionTypeError(action_type, i + 1)
        return default_actions

    def create_listener(
        self,
        load_balancer_arn,
        protocol,
        port,
        ssl_policy,
        certificate,
        default_actions,
    ):
        default_actions = [FakeAction(action) for action in default_actions]
        balancer = self.load_balancers.get(load_balancer_arn)
        if balancer is None:
            raise LoadBalancerNotFoundError()
        if port in balancer.listeners:
            raise DuplicateListenerError()

        self._validate_actions(default_actions)

        arn = load_balancer_arn.replace(":loadbalancer/", ":listener/") + "/%s%s" % (
            port,
            id(self),
        )
        listener = FakeListener(
            load_balancer_arn,
            arn,
            protocol,
            port,
            ssl_policy,
            certificate,
            default_actions,
        )
        balancer.listeners[listener.arn] = listener
        for action in default_actions:
            if action.type == "forward":
                found_arns = self._get_target_group_arns_from(action_data=action.data)
                for arn in found_arns:
                    target_group = self.target_groups[arn]
                    target_group.load_balancer_arns.append(load_balancer_arn)

        return listener

    def describe_load_balancers(self, arns, names):
        balancers = self.load_balancers.values()
        arns = arns or []
        names = names or []
        if not arns and not names:
            for balancer in balancers:
                balancer.activate()
            return balancers

        matched_balancers = []
        matched_balancer = None

        for arn in arns:
            for balancer in balancers:
                balancer.activate()
                if balancer.arn == arn:
                    matched_balancer = balancer
            if matched_balancer is None:
                raise LoadBalancerNotFoundError()
            elif matched_balancer not in matched_balancers:
                matched_balancers.append(matched_balancer)

        for name in names:
            for balancer in balancers:
                balancer.activate()
                if balancer.name == name:
                    matched_balancer = balancer
            if matched_balancer is None:
                raise LoadBalancerNotFoundError()
            elif matched_balancer not in matched_balancers:
                matched_balancers.append(matched_balancer)

        return matched_balancers

    def describe_rules(self, listener_arn, rule_arns):
        if listener_arn is None and not rule_arns:
            raise InvalidDescribeRulesRequest(
                "You must specify either listener rule ARNs or a listener ARN"
            )
        if listener_arn is not None and rule_arns is not None:
            raise InvalidDescribeRulesRequest(
                "Listener rule ARNs and a listener ARN cannot be specified at the same time"
            )
        if listener_arn:
            listener = self.describe_listeners(None, [listener_arn])[0]
            return listener.rules.values()

        # search for rule arns
        matched_rules = []
        for load_balancer_arn in self.load_balancers:
            listeners = self.load_balancers.get(load_balancer_arn).listeners.values()
            for listener in listeners:
                for rule in listener.rules.values():
                    if rule.arn in rule_arns:
                        matched_rules.append(rule)
        return matched_rules

    def describe_target_groups(self, load_balancer_arn, target_group_arns, names):
        if load_balancer_arn:
            if load_balancer_arn not in self.load_balancers:
                raise LoadBalancerNotFoundError()
            return [
                tg
                for tg in self.target_groups.values()
                if load_balancer_arn in tg.load_balancer_arns
            ]

        if target_group_arns:
            try:
                return [self.target_groups[arn] for arn in target_group_arns]
            except KeyError:
                raise TargetGroupNotFoundError()
        if names:
            matched = []
            for name in names:
                found = None
                for target_group in self.target_groups.values():
                    if target_group.name == name:
                        found = target_group
                if not found:
                    raise TargetGroupNotFoundError()
                matched.append(found)
            return matched

        return self.target_groups.values()

    def describe_listeners(self, load_balancer_arn, listener_arns):
        if load_balancer_arn:
            if load_balancer_arn not in self.load_balancers:
                raise LoadBalancerNotFoundError()
            return self.load_balancers.get(load_balancer_arn).listeners.values()

        matched = []
        for load_balancer in self.load_balancers.values():
            for listener_arn in listener_arns:
                listener = load_balancer.listeners.get(listener_arn)
                if listener:
                    matched.append(listener)
        if listener_arns and len(matched) == 0:
            raise ListenerNotFoundError()
        return matched

    def delete_load_balancer(self, arn):
        self.load_balancers.pop(arn, None)

    def delete_rule(self, arn):
        for load_balancer_arn in self.load_balancers:
            listeners = self.load_balancers.get(load_balancer_arn).listeners.values()
            for listener in listeners:
                for rule in listener.rules.values():
                    if rule.arn == arn:
                        listener.remove_rule(rule.arn)
                        return

        # should raise RuleNotFound Error according to the AWS API doc
        # however, boto3 does't raise error even if rule is not found

    def delete_target_group(self, target_group_arn):
        if target_group_arn not in self.target_groups:
            raise TargetGroupNotFoundError()

        target_group = self.target_groups[target_group_arn]
        if target_group:
            if self._any_listener_using(target_group_arn):
                raise ResourceInUseError(
                    "The target group '{}' is currently in use by a listener or a rule".format(
                        target_group_arn
                    )
                )
            del self.target_groups[target_group_arn]
            return target_group

    def delete_listener(self, listener_arn):
        for load_balancer in self.load_balancers.values():
            listener = load_balancer.listeners.pop(listener_arn, None)
            if listener:
                return listener
        raise ListenerNotFoundError()

    def modify_rule(self, rule_arn, conditions, actions):
        actions = [FakeAction(action) for action in actions]
        # if conditions or actions is empty list, do not update the attributes
        if not conditions and not actions:
            raise InvalidModifyRuleArgumentsError()
        rules = self.describe_rules(listener_arn=None, rule_arns=[rule_arn])
        if not rules:
            raise RuleNotFoundError()
        rule = rules[0]

        self._validate_conditions(conditions)
        # TODO: check pattern of value for 'host-header'
        # TODO: check pattern of value for 'path-pattern'

        # validate Actions
        self._validate_actions(actions)

        # TODO: check for error 'TooManyRegistrationsForTargetId'
        # TODO: check for error 'TooManyRules'

        # modify rule
        if conditions:
            rule.conditions = conditions
        if actions:
            rule.actions = actions
        return rule

    def register_targets(self, target_group_arn, instances):
        target_group = self.target_groups.get(target_group_arn)
        if target_group is None:
            raise TargetGroupNotFoundError()
        target_group.register(instances)

    def deregister_targets(self, target_group_arn, instances):
        target_group = self.target_groups.get(target_group_arn)
        if target_group is None:
            raise TargetGroupNotFoundError()
        target_group.deregister(instances)

    def describe_target_health(self, target_group_arn, targets):
        target_group = self.target_groups.get(target_group_arn)
        if target_group is None:
            raise TargetGroupNotFoundError()

        if not targets:
            targets = target_group.targets.values()
        return [target_group.health_for(target, self.ec2_backend) for target in targets]

    def set_rule_priorities(self, rule_priorities):
        # validate
        priorities = [rule_priority["priority"] for rule_priority in rule_priorities]
        for priority in set(priorities):
            if priorities.count(priority) > 1:
                raise DuplicatePriorityError(priority)

        # validate
        for rule_priority in rule_priorities:
            given_rule_arn = rule_priority["rule_arn"]
            priority = rule_priority["priority"]
            _given_rules = self.describe_rules(
                listener_arn=None, rule_arns=[given_rule_arn]
            )
            if not _given_rules:
                raise RuleNotFoundError()
            given_rule = _given_rules[0]
            listeners = self.describe_listeners(None, [given_rule.listener_arn])
            listener = listeners[0]
            for rule_in_listener in listener.rules.values():
                if rule_in_listener.priority == priority:
                    raise PriorityInUseError()
        # modify
        modified_rules = []
        for rule_priority in rule_priorities:
            given_rule_arn = rule_priority["rule_arn"]
            priority = rule_priority["priority"]
            _given_rules = self.describe_rules(
                listener_arn=None, rule_arns=[given_rule_arn]
            )
            if not _given_rules:
                raise RuleNotFoundError()
            given_rule = _given_rules[0]
            given_rule.priority = priority
            modified_rules.append(given_rule)
        return modified_rules

    def set_ip_address_type(self, arn, ip_type):
        if ip_type not in ("internal", "dualstack"):
            raise RESTError(
                "InvalidParameterValue",
                "IpAddressType must be either internal | dualstack",
            )

        balancer = self.load_balancers.get(arn)
        if balancer is None:
            raise LoadBalancerNotFoundError()

        if ip_type == "dualstack" and balancer.scheme == "internal":
            raise RESTError(
                "InvalidConfigurationRequest",
                "Internal load balancers cannot be dualstack",
            )

        balancer.stack = ip_type

    def set_security_groups(self, arn, sec_groups):
        balancer = self.load_balancers.get(arn)
        if balancer is None:
            raise LoadBalancerNotFoundError()

        # Check all security groups exist
        for sec_group_id in sec_groups:
            if self.ec2_backend.get_security_group_from_id(sec_group_id) is None:
                raise RESTError(
                    "InvalidSecurityGroup",
                    "Security group {0} does not exist".format(sec_group_id),
                )

        balancer.security_groups = sec_groups

    def set_subnets(self, arn, subnets):
        balancer = self.load_balancers.get(arn)
        if balancer is None:
            raise LoadBalancerNotFoundError()

        subnet_objects = []
        sub_zone_list = {}
        for subnet in subnets:
            try:
                subnet = self.ec2_backend.get_subnet(subnet)

                if subnet.availability_zone in sub_zone_list:
                    raise RESTError(
                        "InvalidConfigurationRequest",
                        "More than 1 subnet cannot be specified for 1 availability zone",
                    )

                sub_zone_list[subnet.availability_zone] = subnet.id
                subnet_objects.append(subnet)
            except Exception:
                raise SubnetNotFoundError()

        if len(sub_zone_list) < 2:
            raise RESTError(
                "InvalidConfigurationRequest",
                "More than 1 availability zone must be specified",
            )

        balancer.subnets = subnet_objects

        return sub_zone_list.items()

    def modify_load_balancer_attributes(self, arn, attrs):
        balancer = self.load_balancers.get(arn)
        if balancer is None:
            raise LoadBalancerNotFoundError()

        for key in attrs:
            if key not in FakeLoadBalancer.VALID_ATTRS:
                raise RESTError(
                    "InvalidConfigurationRequest", "Key {0} not valid".format(key)
                )

        balancer.attrs.update(attrs)
        return balancer.attrs

    def describe_load_balancer_attributes(self, arn):
        balancer = self.load_balancers.get(arn)
        if balancer is None:
            raise LoadBalancerNotFoundError()

        return balancer.attrs

    def modify_target_group(
        self,
        arn,
        health_check_proto=None,
        health_check_port=None,
        health_check_path=None,
        health_check_interval=None,
        health_check_timeout=None,
        healthy_threshold_count=None,
        unhealthy_threshold_count=None,
        http_codes=None,
        health_check_enabled=None,
    ):
        target_group = self.target_groups.get(arn)
        if target_group is None:
            raise TargetGroupNotFoundError()

        if (
            http_codes is not None
            and FakeTargetGroup.HTTP_CODE_REGEX.match(http_codes) is None
        ):
            raise RESTError(
                "InvalidParameterValue",
                "HttpCode must be like 200 | 200-399 | 200,201 ...",
            )

        if http_codes is not None and target_group.protocol in ["HTTP", "HTTPS"]:
            target_group.matcher["HttpCode"] = http_codes
        if health_check_interval is not None:
            target_group.healthcheck_interval_seconds = health_check_interval
        if health_check_path is not None:
            target_group.healthcheck_path = health_check_path
        if health_check_port is not None:
            target_group.healthcheck_port = health_check_port
        if health_check_proto is not None:
            target_group.healthcheck_protocol = health_check_proto
        if health_check_timeout is not None:
            target_group.healthcheck_timeout_seconds = health_check_timeout
        if health_check_enabled is not None:
            target_group.healthcheck_enabled = health_check_enabled
        if healthy_threshold_count is not None:
            target_group.healthy_threshold_count = healthy_threshold_count
        if unhealthy_threshold_count is not None:
            target_group.unhealthy_threshold_count = unhealthy_threshold_count

        return target_group

    def modify_listener(
        self,
        arn,
        port=None,
        protocol=None,
        ssl_policy=None,
        certificates=None,
        default_actions=None,
    ):
        default_actions = [FakeAction(action) for action in default_actions]
        for load_balancer in self.load_balancers.values():
            if arn in load_balancer.listeners:
                break
        else:
            raise ListenerNotFoundError()

        listener = load_balancer.listeners[arn]

        if port is not None:
            listener.port = port

        if protocol not in (None, "HTTP", "HTTPS", "TCP"):
            raise RESTError(
                "UnsupportedProtocol", "Protocol {0} is not supported".format(protocol),
            )

        # HTTPS checks
        protocol_becomes_https = protocol == "HTTPS"
        protocol_stays_https = protocol is None and listener.protocol == "HTTPS"
        if protocol_becomes_https or protocol_stays_https:
            # Check certificates exist
            if certificates:
                default_cert = certificates[0]
                default_cert_arn = default_cert["certificate_arn"]
                try:
                    self.acm_backend.get_certificate(default_cert_arn)
                except Exception:
                    raise RESTError(
                        "CertificateNotFound",
                        "Certificate {0} not found".format(default_cert_arn),
                    )
                listener.certificate = default_cert_arn
                listener.certificates = certificates
            else:
                raise RESTError(
                    "CertificateWereNotPassed",
                    "You must provide a list containing exactly one certificate if the listener protocol is HTTPS.",
                )

        if protocol is not None:
            listener.protocol = protocol

        if ssl_policy is not None:
            # Its already validated in responses.py
            listener.ssl_policy = ssl_policy

        if default_actions is not None and default_actions != []:
            # Is currently not validated
            listener.default_actions = default_actions
            listener._default_rule[0].actions = default_actions

        return listener

    def _any_listener_using(self, target_group_arn):
        for load_balancer in self.load_balancers.values():
            for listener in load_balancer.listeners.values():
                for rule in listener.rules.values():
                    for action in rule.actions:
                        found_arns = self._get_target_group_arns_from(
                            action_data=action.data
                        )
                        if target_group_arn in found_arns:
                            return True
        return False

    def notify_terminate_instances(self, instance_ids):
        for target_group in self.target_groups.values():
            target_group.deregister_terminated_instances(instance_ids)


elbv2_backends = BackendDict(ELBv2Backend, "ec2")
