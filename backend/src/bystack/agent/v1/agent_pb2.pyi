from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Slice(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SLICE_UNSPECIFIED: _ClassVar[Slice]
    SLICE_CONTAINER: _ClassVar[Slice]
    SLICE_NETWORK: _ClassVar[Slice]
    SLICE_VOLUME: _ClassVar[Slice]
    SLICE_IMAGE: _ClassVar[Slice]
SLICE_UNSPECIFIED: Slice
SLICE_CONTAINER: Slice
SLICE_NETWORK: Slice
SLICE_VOLUME: Slice
SLICE_IMAGE: Slice

class Envelope(_message.Message):
    __slots__ = ("seq", "hello", "hello_ack", "sync", "delta", "command", "command_result", "resync_request", "logs_request", "logs_response", "enroll_request", "enroll_response", "renewal_offer", "certificate_request", "certificate_issued")
    SEQ_FIELD_NUMBER: _ClassVar[int]
    HELLO_FIELD_NUMBER: _ClassVar[int]
    HELLO_ACK_FIELD_NUMBER: _ClassVar[int]
    SYNC_FIELD_NUMBER: _ClassVar[int]
    DELTA_FIELD_NUMBER: _ClassVar[int]
    COMMAND_FIELD_NUMBER: _ClassVar[int]
    COMMAND_RESULT_FIELD_NUMBER: _ClassVar[int]
    RESYNC_REQUEST_FIELD_NUMBER: _ClassVar[int]
    LOGS_REQUEST_FIELD_NUMBER: _ClassVar[int]
    LOGS_RESPONSE_FIELD_NUMBER: _ClassVar[int]
    ENROLL_REQUEST_FIELD_NUMBER: _ClassVar[int]
    ENROLL_RESPONSE_FIELD_NUMBER: _ClassVar[int]
    RENEWAL_OFFER_FIELD_NUMBER: _ClassVar[int]
    CERTIFICATE_REQUEST_FIELD_NUMBER: _ClassVar[int]
    CERTIFICATE_ISSUED_FIELD_NUMBER: _ClassVar[int]
    seq: int
    hello: Hello
    hello_ack: HelloAck
    sync: Sync
    delta: Delta
    command: Command
    command_result: CommandResult
    resync_request: ResyncRequest
    logs_request: LogsRequest
    logs_response: LogsResponse
    enroll_request: EnrollRequest
    enroll_response: EnrollResponse
    renewal_offer: RenewalOffer
    certificate_request: CertificateRequest
    certificate_issued: CertificateIssued
    def __init__(self, seq: _Optional[int] = ..., hello: _Optional[_Union[Hello, _Mapping]] = ..., hello_ack: _Optional[_Union[HelloAck, _Mapping]] = ..., sync: _Optional[_Union[Sync, _Mapping]] = ..., delta: _Optional[_Union[Delta, _Mapping]] = ..., command: _Optional[_Union[Command, _Mapping]] = ..., command_result: _Optional[_Union[CommandResult, _Mapping]] = ..., resync_request: _Optional[_Union[ResyncRequest, _Mapping]] = ..., logs_request: _Optional[_Union[LogsRequest, _Mapping]] = ..., logs_response: _Optional[_Union[LogsResponse, _Mapping]] = ..., enroll_request: _Optional[_Union[EnrollRequest, _Mapping]] = ..., enroll_response: _Optional[_Union[EnrollResponse, _Mapping]] = ..., renewal_offer: _Optional[_Union[RenewalOffer, _Mapping]] = ..., certificate_request: _Optional[_Union[CertificateRequest, _Mapping]] = ..., certificate_issued: _Optional[_Union[CertificateIssued, _Mapping]] = ...) -> None: ...

class Hello(_message.Message):
    __slots__ = ("agent_version", "engine_id", "engine", "read_only", "capabilities", "unix_time")
    AGENT_VERSION_FIELD_NUMBER: _ClassVar[int]
    ENGINE_ID_FIELD_NUMBER: _ClassVar[int]
    ENGINE_FIELD_NUMBER: _ClassVar[int]
    READ_ONLY_FIELD_NUMBER: _ClassVar[int]
    CAPABILITIES_FIELD_NUMBER: _ClassVar[int]
    UNIX_TIME_FIELD_NUMBER: _ClassVar[int]
    agent_version: str
    engine_id: str
    engine: EngineInfo
    read_only: bool
    capabilities: _containers.RepeatedScalarFieldContainer[str]
    unix_time: int
    def __init__(self, agent_version: _Optional[str] = ..., engine_id: _Optional[str] = ..., engine: _Optional[_Union[EngineInfo, _Mapping]] = ..., read_only: _Optional[bool] = ..., capabilities: _Optional[_Iterable[str]] = ..., unix_time: _Optional[int] = ...) -> None: ...

class HelloAck(_message.Message):
    __slots__ = ("controller_epoch", "resync_interval", "accepted", "reason", "feature_flags", "retry")
    CONTROLLER_EPOCH_FIELD_NUMBER: _ClassVar[int]
    RESYNC_INTERVAL_FIELD_NUMBER: _ClassVar[int]
    ACCEPTED_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    FEATURE_FLAGS_FIELD_NUMBER: _ClassVar[int]
    RETRY_FIELD_NUMBER: _ClassVar[int]
    controller_epoch: str
    resync_interval: int
    accepted: bool
    reason: str
    feature_flags: _containers.RepeatedScalarFieldContainer[str]
    retry: bool
    def __init__(self, controller_epoch: _Optional[str] = ..., resync_interval: _Optional[int] = ..., accepted: _Optional[bool] = ..., reason: _Optional[str] = ..., feature_flags: _Optional[_Iterable[str]] = ..., retry: _Optional[bool] = ...) -> None: ...

class EnrollRequest(_message.Message):
    __slots__ = ("join_token", "engine_id", "csr_pem", "agent_version")
    JOIN_TOKEN_FIELD_NUMBER: _ClassVar[int]
    ENGINE_ID_FIELD_NUMBER: _ClassVar[int]
    CSR_PEM_FIELD_NUMBER: _ClassVar[int]
    AGENT_VERSION_FIELD_NUMBER: _ClassVar[int]
    join_token: str
    engine_id: str
    csr_pem: str
    agent_version: str
    def __init__(self, join_token: _Optional[str] = ..., engine_id: _Optional[str] = ..., csr_pem: _Optional[str] = ..., agent_version: _Optional[str] = ...) -> None: ...

class EnrollResponse(_message.Message):
    __slots__ = ("accepted", "reason", "certificate_pem", "ca_pem", "not_after", "pending_approval")
    ACCEPTED_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    CERTIFICATE_PEM_FIELD_NUMBER: _ClassVar[int]
    CA_PEM_FIELD_NUMBER: _ClassVar[int]
    NOT_AFTER_FIELD_NUMBER: _ClassVar[int]
    PENDING_APPROVAL_FIELD_NUMBER: _ClassVar[int]
    accepted: bool
    reason: str
    certificate_pem: str
    ca_pem: str
    not_after: int
    pending_approval: bool
    def __init__(self, accepted: _Optional[bool] = ..., reason: _Optional[str] = ..., certificate_pem: _Optional[str] = ..., ca_pem: _Optional[str] = ..., not_after: _Optional[int] = ..., pending_approval: _Optional[bool] = ...) -> None: ...

class RenewalOffer(_message.Message):
    __slots__ = ("not_after",)
    NOT_AFTER_FIELD_NUMBER: _ClassVar[int]
    not_after: int
    def __init__(self, not_after: _Optional[int] = ...) -> None: ...

class CertificateRequest(_message.Message):
    __slots__ = ("csr_pem",)
    CSR_PEM_FIELD_NUMBER: _ClassVar[int]
    csr_pem: str
    def __init__(self, csr_pem: _Optional[str] = ...) -> None: ...

class CertificateIssued(_message.Message):
    __slots__ = ("ok", "reason", "certificate_pem", "ca_pem", "not_after")
    OK_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    CERTIFICATE_PEM_FIELD_NUMBER: _ClassVar[int]
    CA_PEM_FIELD_NUMBER: _ClassVar[int]
    NOT_AFTER_FIELD_NUMBER: _ClassVar[int]
    ok: bool
    reason: str
    certificate_pem: str
    ca_pem: str
    not_after: int
    def __init__(self, ok: _Optional[bool] = ..., reason: _Optional[str] = ..., certificate_pem: _Optional[str] = ..., ca_pem: _Optional[str] = ..., not_after: _Optional[int] = ...) -> None: ...

class Sync(_message.Message):
    __slots__ = ("slice", "entities")
    SLICE_FIELD_NUMBER: _ClassVar[int]
    ENTITIES_FIELD_NUMBER: _ClassVar[int]
    slice: Slice
    entities: _containers.RepeatedCompositeFieldContainer[Entity]
    def __init__(self, slice: _Optional[_Union[Slice, str]] = ..., entities: _Optional[_Iterable[_Union[Entity, _Mapping]]] = ...) -> None: ...

class Delta(_message.Message):
    __slots__ = ("slice", "ids", "changed")
    SLICE_FIELD_NUMBER: _ClassVar[int]
    IDS_FIELD_NUMBER: _ClassVar[int]
    CHANGED_FIELD_NUMBER: _ClassVar[int]
    slice: Slice
    ids: _containers.RepeatedScalarFieldContainer[str]
    changed: _containers.RepeatedCompositeFieldContainer[Entity]
    def __init__(self, slice: _Optional[_Union[Slice, str]] = ..., ids: _Optional[_Iterable[str]] = ..., changed: _Optional[_Iterable[_Union[Entity, _Mapping]]] = ...) -> None: ...

class Entity(_message.Message):
    __slots__ = ("id", "container", "network", "volume", "image")
    ID_FIELD_NUMBER: _ClassVar[int]
    CONTAINER_FIELD_NUMBER: _ClassVar[int]
    NETWORK_FIELD_NUMBER: _ClassVar[int]
    VOLUME_FIELD_NUMBER: _ClassVar[int]
    IMAGE_FIELD_NUMBER: _ClassVar[int]
    id: str
    container: Container
    network: Network
    volume: Volume
    image: Image
    def __init__(self, id: _Optional[str] = ..., container: _Optional[_Union[Container, _Mapping]] = ..., network: _Optional[_Union[Network, _Mapping]] = ..., volume: _Optional[_Union[Volume, _Mapping]] = ..., image: _Optional[_Union[Image, _Mapping]] = ...) -> None: ...

class EngineInfo(_message.Message):
    __slots__ = ("id", "name", "server_version", "operating_system", "kernel_version", "architecture", "ncpu", "mem_total", "containers_running", "containers_total")
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    SERVER_VERSION_FIELD_NUMBER: _ClassVar[int]
    OPERATING_SYSTEM_FIELD_NUMBER: _ClassVar[int]
    KERNEL_VERSION_FIELD_NUMBER: _ClassVar[int]
    ARCHITECTURE_FIELD_NUMBER: _ClassVar[int]
    NCPU_FIELD_NUMBER: _ClassVar[int]
    MEM_TOTAL_FIELD_NUMBER: _ClassVar[int]
    CONTAINERS_RUNNING_FIELD_NUMBER: _ClassVar[int]
    CONTAINERS_TOTAL_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    server_version: str
    operating_system: str
    kernel_version: str
    architecture: str
    ncpu: int
    mem_total: int
    containers_running: int
    containers_total: int
    def __init__(self, id: _Optional[str] = ..., name: _Optional[str] = ..., server_version: _Optional[str] = ..., operating_system: _Optional[str] = ..., kernel_version: _Optional[str] = ..., architecture: _Optional[str] = ..., ncpu: _Optional[int] = ..., mem_total: _Optional[int] = ..., containers_running: _Optional[int] = ..., containers_total: _Optional[int] = ...) -> None: ...

class Container(_message.Message):
    __slots__ = ("id", "names", "image", "image_id", "command", "created", "state", "status_text", "labels", "ports", "networks", "mounts", "health", "restart_count")
    class LabelsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    ID_FIELD_NUMBER: _ClassVar[int]
    NAMES_FIELD_NUMBER: _ClassVar[int]
    IMAGE_FIELD_NUMBER: _ClassVar[int]
    IMAGE_ID_FIELD_NUMBER: _ClassVar[int]
    COMMAND_FIELD_NUMBER: _ClassVar[int]
    CREATED_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    STATUS_TEXT_FIELD_NUMBER: _ClassVar[int]
    LABELS_FIELD_NUMBER: _ClassVar[int]
    PORTS_FIELD_NUMBER: _ClassVar[int]
    NETWORKS_FIELD_NUMBER: _ClassVar[int]
    MOUNTS_FIELD_NUMBER: _ClassVar[int]
    HEALTH_FIELD_NUMBER: _ClassVar[int]
    RESTART_COUNT_FIELD_NUMBER: _ClassVar[int]
    id: str
    names: _containers.RepeatedScalarFieldContainer[str]
    image: str
    image_id: str
    command: str
    created: int
    state: str
    status_text: str
    labels: _containers.ScalarMap[str, str]
    ports: _containers.RepeatedCompositeFieldContainer[Port]
    networks: _containers.RepeatedCompositeFieldContainer[NetworkAttachment]
    mounts: _containers.RepeatedCompositeFieldContainer[Mount]
    health: str
    restart_count: int
    def __init__(self, id: _Optional[str] = ..., names: _Optional[_Iterable[str]] = ..., image: _Optional[str] = ..., image_id: _Optional[str] = ..., command: _Optional[str] = ..., created: _Optional[int] = ..., state: _Optional[str] = ..., status_text: _Optional[str] = ..., labels: _Optional[_Mapping[str, str]] = ..., ports: _Optional[_Iterable[_Union[Port, _Mapping]]] = ..., networks: _Optional[_Iterable[_Union[NetworkAttachment, _Mapping]]] = ..., mounts: _Optional[_Iterable[_Union[Mount, _Mapping]]] = ..., health: _Optional[str] = ..., restart_count: _Optional[int] = ...) -> None: ...

class Port(_message.Message):
    __slots__ = ("private_port", "public_port", "protocol", "host_ip")
    PRIVATE_PORT_FIELD_NUMBER: _ClassVar[int]
    PUBLIC_PORT_FIELD_NUMBER: _ClassVar[int]
    PROTOCOL_FIELD_NUMBER: _ClassVar[int]
    HOST_IP_FIELD_NUMBER: _ClassVar[int]
    private_port: int
    public_port: int
    protocol: str
    host_ip: str
    def __init__(self, private_port: _Optional[int] = ..., public_port: _Optional[int] = ..., protocol: _Optional[str] = ..., host_ip: _Optional[str] = ...) -> None: ...

class NetworkAttachment(_message.Message):
    __slots__ = ("network_id", "name", "ipv4", "aliases")
    NETWORK_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    IPV4_FIELD_NUMBER: _ClassVar[int]
    ALIASES_FIELD_NUMBER: _ClassVar[int]
    network_id: str
    name: str
    ipv4: str
    aliases: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, network_id: _Optional[str] = ..., name: _Optional[str] = ..., ipv4: _Optional[str] = ..., aliases: _Optional[_Iterable[str]] = ...) -> None: ...

class Mount(_message.Message):
    __slots__ = ("type", "name", "destination", "mode", "rw")
    TYPE_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    DESTINATION_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    RW_FIELD_NUMBER: _ClassVar[int]
    type: str
    name: str
    destination: str
    mode: str
    rw: bool
    def __init__(self, type: _Optional[str] = ..., name: _Optional[str] = ..., destination: _Optional[str] = ..., mode: _Optional[str] = ..., rw: _Optional[bool] = ...) -> None: ...

class Network(_message.Message):
    __slots__ = ("id", "name", "driver", "scope", "internal", "attachable", "ingress", "subnets", "labels")
    class LabelsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    DRIVER_FIELD_NUMBER: _ClassVar[int]
    SCOPE_FIELD_NUMBER: _ClassVar[int]
    INTERNAL_FIELD_NUMBER: _ClassVar[int]
    ATTACHABLE_FIELD_NUMBER: _ClassVar[int]
    INGRESS_FIELD_NUMBER: _ClassVar[int]
    SUBNETS_FIELD_NUMBER: _ClassVar[int]
    LABELS_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    driver: str
    scope: str
    internal: bool
    attachable: bool
    ingress: bool
    subnets: _containers.RepeatedScalarFieldContainer[str]
    labels: _containers.ScalarMap[str, str]
    def __init__(self, id: _Optional[str] = ..., name: _Optional[str] = ..., driver: _Optional[str] = ..., scope: _Optional[str] = ..., internal: _Optional[bool] = ..., attachable: _Optional[bool] = ..., ingress: _Optional[bool] = ..., subnets: _Optional[_Iterable[str]] = ..., labels: _Optional[_Mapping[str, str]] = ...) -> None: ...

class Volume(_message.Message):
    __slots__ = ("name", "driver", "mountpoint", "scope", "created_at", "labels")
    class LabelsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    NAME_FIELD_NUMBER: _ClassVar[int]
    DRIVER_FIELD_NUMBER: _ClassVar[int]
    MOUNTPOINT_FIELD_NUMBER: _ClassVar[int]
    SCOPE_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    LABELS_FIELD_NUMBER: _ClassVar[int]
    name: str
    driver: str
    mountpoint: str
    scope: str
    created_at: str
    labels: _containers.ScalarMap[str, str]
    def __init__(self, name: _Optional[str] = ..., driver: _Optional[str] = ..., mountpoint: _Optional[str] = ..., scope: _Optional[str] = ..., created_at: _Optional[str] = ..., labels: _Optional[_Mapping[str, str]] = ...) -> None: ...

class Image(_message.Message):
    __slots__ = ("id", "repo_tags", "repo_digests", "size", "created", "labels")
    class LabelsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    ID_FIELD_NUMBER: _ClassVar[int]
    REPO_TAGS_FIELD_NUMBER: _ClassVar[int]
    REPO_DIGESTS_FIELD_NUMBER: _ClassVar[int]
    SIZE_FIELD_NUMBER: _ClassVar[int]
    CREATED_FIELD_NUMBER: _ClassVar[int]
    LABELS_FIELD_NUMBER: _ClassVar[int]
    id: str
    repo_tags: _containers.RepeatedScalarFieldContainer[str]
    repo_digests: _containers.RepeatedScalarFieldContainer[str]
    size: int
    created: int
    labels: _containers.ScalarMap[str, str]
    def __init__(self, id: _Optional[str] = ..., repo_tags: _Optional[_Iterable[str]] = ..., repo_digests: _Optional[_Iterable[str]] = ..., size: _Optional[int] = ..., created: _Optional[int] = ..., labels: _Optional[_Mapping[str, str]] = ...) -> None: ...

class Command(_message.Message):
    __slots__ = ("command_id", "verb", "target_id", "args")
    class ArgsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    COMMAND_ID_FIELD_NUMBER: _ClassVar[int]
    VERB_FIELD_NUMBER: _ClassVar[int]
    TARGET_ID_FIELD_NUMBER: _ClassVar[int]
    ARGS_FIELD_NUMBER: _ClassVar[int]
    command_id: str
    verb: str
    target_id: str
    args: _containers.ScalarMap[str, str]
    def __init__(self, command_id: _Optional[str] = ..., verb: _Optional[str] = ..., target_id: _Optional[str] = ..., args: _Optional[_Mapping[str, str]] = ...) -> None: ...

class CommandResult(_message.Message):
    __slots__ = ("command_id", "ok", "detail", "unchanged")
    COMMAND_ID_FIELD_NUMBER: _ClassVar[int]
    OK_FIELD_NUMBER: _ClassVar[int]
    DETAIL_FIELD_NUMBER: _ClassVar[int]
    UNCHANGED_FIELD_NUMBER: _ClassVar[int]
    command_id: str
    ok: bool
    detail: str
    unchanged: bool
    def __init__(self, command_id: _Optional[str] = ..., ok: _Optional[bool] = ..., detail: _Optional[str] = ..., unchanged: _Optional[bool] = ...) -> None: ...

class ResyncRequest(_message.Message):
    __slots__ = ("slices",)
    SLICES_FIELD_NUMBER: _ClassVar[int]
    slices: _containers.RepeatedScalarFieldContainer[Slice]
    def __init__(self, slices: _Optional[_Iterable[_Union[Slice, str]]] = ...) -> None: ...

class LogsRequest(_message.Message):
    __slots__ = ("request_id", "target_id", "tail")
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    TARGET_ID_FIELD_NUMBER: _ClassVar[int]
    TAIL_FIELD_NUMBER: _ClassVar[int]
    request_id: str
    target_id: str
    tail: int
    def __init__(self, request_id: _Optional[str] = ..., target_id: _Optional[str] = ..., tail: _Optional[int] = ...) -> None: ...

class LogsResponse(_message.Message):
    __slots__ = ("request_id", "ok", "reason", "lines")
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    OK_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    LINES_FIELD_NUMBER: _ClassVar[int]
    request_id: str
    ok: bool
    reason: str
    lines: _containers.RepeatedCompositeFieldContainer[LogLine]
    def __init__(self, request_id: _Optional[str] = ..., ok: _Optional[bool] = ..., reason: _Optional[str] = ..., lines: _Optional[_Iterable[_Union[LogLine, _Mapping]]] = ...) -> None: ...

class LogLine(_message.Message):
    __slots__ = ("stderr", "text")
    STDERR_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    stderr: bool
    text: str
    def __init__(self, stderr: _Optional[bool] = ..., text: _Optional[str] = ...) -> None: ...
