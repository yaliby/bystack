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
    SLICE_UNIT: _ClassVar[Slice]
    SLICE_PROCESS: _ClassVar[Slice]
SLICE_UNSPECIFIED: Slice
SLICE_CONTAINER: Slice
SLICE_NETWORK: Slice
SLICE_VOLUME: Slice
SLICE_IMAGE: Slice
SLICE_UNIT: Slice
SLICE_PROCESS: Slice

class Envelope(_message.Message):
    __slots__ = ("seq", "hello", "hello_ack", "sync", "delta", "command", "command_result", "resync_request", "logs_request", "logs_response", "logs_subscribe", "logs_chunk", "logs_cancel", "enroll_request", "enroll_response", "renewal_offer", "certificate_request", "certificate_issued", "watch_list", "inventory_request", "inventory_response", "upgrade_offer", "upgrade_chunk", "upgrade_status")
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
    LOGS_SUBSCRIBE_FIELD_NUMBER: _ClassVar[int]
    LOGS_CHUNK_FIELD_NUMBER: _ClassVar[int]
    LOGS_CANCEL_FIELD_NUMBER: _ClassVar[int]
    ENROLL_REQUEST_FIELD_NUMBER: _ClassVar[int]
    ENROLL_RESPONSE_FIELD_NUMBER: _ClassVar[int]
    RENEWAL_OFFER_FIELD_NUMBER: _ClassVar[int]
    CERTIFICATE_REQUEST_FIELD_NUMBER: _ClassVar[int]
    CERTIFICATE_ISSUED_FIELD_NUMBER: _ClassVar[int]
    WATCH_LIST_FIELD_NUMBER: _ClassVar[int]
    INVENTORY_REQUEST_FIELD_NUMBER: _ClassVar[int]
    INVENTORY_RESPONSE_FIELD_NUMBER: _ClassVar[int]
    UPGRADE_OFFER_FIELD_NUMBER: _ClassVar[int]
    UPGRADE_CHUNK_FIELD_NUMBER: _ClassVar[int]
    UPGRADE_STATUS_FIELD_NUMBER: _ClassVar[int]
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
    logs_subscribe: LogsSubscribe
    logs_chunk: LogsChunk
    logs_cancel: LogsCancel
    enroll_request: EnrollRequest
    enroll_response: EnrollResponse
    renewal_offer: RenewalOffer
    certificate_request: CertificateRequest
    certificate_issued: CertificateIssued
    watch_list: WatchList
    inventory_request: InventoryRequest
    inventory_response: InventoryResponse
    upgrade_offer: UpgradeOffer
    upgrade_chunk: UpgradeChunk
    upgrade_status: UpgradeStatus
    def __init__(self, seq: _Optional[int] = ..., hello: _Optional[_Union[Hello, _Mapping]] = ..., hello_ack: _Optional[_Union[HelloAck, _Mapping]] = ..., sync: _Optional[_Union[Sync, _Mapping]] = ..., delta: _Optional[_Union[Delta, _Mapping]] = ..., command: _Optional[_Union[Command, _Mapping]] = ..., command_result: _Optional[_Union[CommandResult, _Mapping]] = ..., resync_request: _Optional[_Union[ResyncRequest, _Mapping]] = ..., logs_request: _Optional[_Union[LogsRequest, _Mapping]] = ..., logs_response: _Optional[_Union[LogsResponse, _Mapping]] = ..., logs_subscribe: _Optional[_Union[LogsSubscribe, _Mapping]] = ..., logs_chunk: _Optional[_Union[LogsChunk, _Mapping]] = ..., logs_cancel: _Optional[_Union[LogsCancel, _Mapping]] = ..., enroll_request: _Optional[_Union[EnrollRequest, _Mapping]] = ..., enroll_response: _Optional[_Union[EnrollResponse, _Mapping]] = ..., renewal_offer: _Optional[_Union[RenewalOffer, _Mapping]] = ..., certificate_request: _Optional[_Union[CertificateRequest, _Mapping]] = ..., certificate_issued: _Optional[_Union[CertificateIssued, _Mapping]] = ..., watch_list: _Optional[_Union[WatchList, _Mapping]] = ..., inventory_request: _Optional[_Union[InventoryRequest, _Mapping]] = ..., inventory_response: _Optional[_Union[InventoryResponse, _Mapping]] = ..., upgrade_offer: _Optional[_Union[UpgradeOffer, _Mapping]] = ..., upgrade_chunk: _Optional[_Union[UpgradeChunk, _Mapping]] = ..., upgrade_status: _Optional[_Union[UpgradeStatus, _Mapping]] = ...) -> None: ...

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
    __slots__ = ("id", "container", "network", "volume", "image", "unit", "process")
    ID_FIELD_NUMBER: _ClassVar[int]
    CONTAINER_FIELD_NUMBER: _ClassVar[int]
    NETWORK_FIELD_NUMBER: _ClassVar[int]
    VOLUME_FIELD_NUMBER: _ClassVar[int]
    IMAGE_FIELD_NUMBER: _ClassVar[int]
    UNIT_FIELD_NUMBER: _ClassVar[int]
    PROCESS_FIELD_NUMBER: _ClassVar[int]
    id: str
    container: Container
    network: Network
    volume: Volume
    image: Image
    unit: Unit
    process: Process
    def __init__(self, id: _Optional[str] = ..., container: _Optional[_Union[Container, _Mapping]] = ..., network: _Optional[_Union[Network, _Mapping]] = ..., volume: _Optional[_Union[Volume, _Mapping]] = ..., image: _Optional[_Union[Image, _Mapping]] = ..., unit: _Optional[_Union[Unit, _Mapping]] = ..., process: _Optional[_Union[Process, _Mapping]] = ...) -> None: ...

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

class WatchList(_message.Message):
    __slots__ = ("entries",)
    ENTRIES_FIELD_NUMBER: _ClassVar[int]
    entries: _containers.RepeatedCompositeFieldContainer[WatchEntry]
    def __init__(self, entries: _Optional[_Iterable[_Union[WatchEntry, _Mapping]]] = ...) -> None: ...

class WatchEntry(_message.Message):
    __slots__ = ("id", "kind", "name", "match_kind", "pattern", "label")
    ID_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    MATCH_KIND_FIELD_NUMBER: _ClassVar[int]
    PATTERN_FIELD_NUMBER: _ClassVar[int]
    LABEL_FIELD_NUMBER: _ClassVar[int]
    id: str
    kind: str
    name: str
    match_kind: str
    pattern: str
    label: str
    def __init__(self, id: _Optional[str] = ..., kind: _Optional[str] = ..., name: _Optional[str] = ..., match_kind: _Optional[str] = ..., pattern: _Optional[str] = ..., label: _Optional[str] = ...) -> None: ...

class Unit(_message.Message):
    __slots__ = ("name", "description", "load_state", "active_state", "sub_state", "unit_file_state", "main_pid", "active_enter_timestamp", "n_restarts", "result", "exec_main_status", "fragment_path")
    NAME_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    LOAD_STATE_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_STATE_FIELD_NUMBER: _ClassVar[int]
    SUB_STATE_FIELD_NUMBER: _ClassVar[int]
    UNIT_FILE_STATE_FIELD_NUMBER: _ClassVar[int]
    MAIN_PID_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_ENTER_TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    N_RESTARTS_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    EXEC_MAIN_STATUS_FIELD_NUMBER: _ClassVar[int]
    FRAGMENT_PATH_FIELD_NUMBER: _ClassVar[int]
    name: str
    description: str
    load_state: str
    active_state: str
    sub_state: str
    unit_file_state: str
    main_pid: int
    active_enter_timestamp: int
    n_restarts: int
    result: str
    exec_main_status: int
    fragment_path: str
    def __init__(self, name: _Optional[str] = ..., description: _Optional[str] = ..., load_state: _Optional[str] = ..., active_state: _Optional[str] = ..., sub_state: _Optional[str] = ..., unit_file_state: _Optional[str] = ..., main_pid: _Optional[int] = ..., active_enter_timestamp: _Optional[int] = ..., n_restarts: _Optional[int] = ..., result: _Optional[str] = ..., exec_main_status: _Optional[int] = ..., fragment_path: _Optional[str] = ...) -> None: ...

class Process(_message.Message):
    __slots__ = ("watch_id", "match_kind", "pattern", "instances", "total", "label")
    WATCH_ID_FIELD_NUMBER: _ClassVar[int]
    MATCH_KIND_FIELD_NUMBER: _ClassVar[int]
    PATTERN_FIELD_NUMBER: _ClassVar[int]
    INSTANCES_FIELD_NUMBER: _ClassVar[int]
    TOTAL_FIELD_NUMBER: _ClassVar[int]
    LABEL_FIELD_NUMBER: _ClassVar[int]
    watch_id: str
    match_kind: str
    pattern: str
    instances: _containers.RepeatedCompositeFieldContainer[ProcessInstance]
    total: int
    label: str
    def __init__(self, watch_id: _Optional[str] = ..., match_kind: _Optional[str] = ..., pattern: _Optional[str] = ..., instances: _Optional[_Iterable[_Union[ProcessInstance, _Mapping]]] = ..., total: _Optional[int] = ..., label: _Optional[str] = ...) -> None: ...

class ProcessInstance(_message.Message):
    __slots__ = ("pid", "comm", "cmdline", "state", "started_at", "uid", "cgroup")
    PID_FIELD_NUMBER: _ClassVar[int]
    COMM_FIELD_NUMBER: _ClassVar[int]
    CMDLINE_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    STARTED_AT_FIELD_NUMBER: _ClassVar[int]
    UID_FIELD_NUMBER: _ClassVar[int]
    CGROUP_FIELD_NUMBER: _ClassVar[int]
    pid: int
    comm: str
    cmdline: str
    state: str
    started_at: int
    uid: int
    cgroup: str
    def __init__(self, pid: _Optional[int] = ..., comm: _Optional[str] = ..., cmdline: _Optional[str] = ..., state: _Optional[str] = ..., started_at: _Optional[int] = ..., uid: _Optional[int] = ..., cgroup: _Optional[str] = ...) -> None: ...

class InventoryRequest(_message.Message):
    __slots__ = ("request_id", "kind", "filter", "limit")
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    FILTER_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    request_id: str
    kind: str
    filter: str
    limit: int
    def __init__(self, request_id: _Optional[str] = ..., kind: _Optional[str] = ..., filter: _Optional[str] = ..., limit: _Optional[int] = ...) -> None: ...

class InventoryResponse(_message.Message):
    __slots__ = ("request_id", "ok", "reason", "items", "total")
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    OK_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    TOTAL_FIELD_NUMBER: _ClassVar[int]
    request_id: str
    ok: bool
    reason: str
    items: _containers.RepeatedCompositeFieldContainer[InventoryItem]
    total: int
    def __init__(self, request_id: _Optional[str] = ..., ok: _Optional[bool] = ..., reason: _Optional[str] = ..., items: _Optional[_Iterable[_Union[InventoryItem, _Mapping]]] = ..., total: _Optional[int] = ...) -> None: ...

class InventoryItem(_message.Message):
    __slots__ = ("id", "name", "description", "state", "detail", "pid")
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    DETAIL_FIELD_NUMBER: _ClassVar[int]
    PID_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    description: str
    state: str
    detail: str
    pid: int
    def __init__(self, id: _Optional[str] = ..., name: _Optional[str] = ..., description: _Optional[str] = ..., state: _Optional[str] = ..., detail: _Optional[str] = ..., pid: _Optional[int] = ...) -> None: ...

class Command(_message.Message):
    __slots__ = ("command_id", "verb", "target_id", "target_kind", "args")
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
    TARGET_KIND_FIELD_NUMBER: _ClassVar[int]
    ARGS_FIELD_NUMBER: _ClassVar[int]
    command_id: str
    verb: str
    target_id: str
    target_kind: str
    args: _containers.ScalarMap[str, str]
    def __init__(self, command_id: _Optional[str] = ..., verb: _Optional[str] = ..., target_id: _Optional[str] = ..., target_kind: _Optional[str] = ..., args: _Optional[_Mapping[str, str]] = ...) -> None: ...

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

class LogsSubscribe(_message.Message):
    __slots__ = ("request_id", "target_id", "tail")
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    TARGET_ID_FIELD_NUMBER: _ClassVar[int]
    TAIL_FIELD_NUMBER: _ClassVar[int]
    request_id: str
    target_id: str
    tail: int
    def __init__(self, request_id: _Optional[str] = ..., target_id: _Optional[str] = ..., tail: _Optional[int] = ...) -> None: ...

class LogsChunk(_message.Message):
    __slots__ = ("request_id", "lines", "done", "reason")
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    LINES_FIELD_NUMBER: _ClassVar[int]
    DONE_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    request_id: str
    lines: _containers.RepeatedCompositeFieldContainer[LogLine]
    done: bool
    reason: str
    def __init__(self, request_id: _Optional[str] = ..., lines: _Optional[_Iterable[_Union[LogLine, _Mapping]]] = ..., done: _Optional[bool] = ..., reason: _Optional[str] = ...) -> None: ...

class LogsCancel(_message.Message):
    __slots__ = ("request_id",)
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    request_id: str
    def __init__(self, request_id: _Optional[str] = ...) -> None: ...

class LogLine(_message.Message):
    __slots__ = ("stderr", "text")
    STDERR_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    stderr: bool
    text: str
    def __init__(self, stderr: _Optional[bool] = ..., text: _Optional[str] = ...) -> None: ...

class UpgradeOffer(_message.Message):
    __slots__ = ("transfer_id", "manifest", "signature", "total_bytes")
    TRANSFER_ID_FIELD_NUMBER: _ClassVar[int]
    MANIFEST_FIELD_NUMBER: _ClassVar[int]
    SIGNATURE_FIELD_NUMBER: _ClassVar[int]
    TOTAL_BYTES_FIELD_NUMBER: _ClassVar[int]
    transfer_id: str
    manifest: bytes
    signature: bytes
    total_bytes: int
    def __init__(self, transfer_id: _Optional[str] = ..., manifest: _Optional[bytes] = ..., signature: _Optional[bytes] = ..., total_bytes: _Optional[int] = ...) -> None: ...

class UpgradeChunk(_message.Message):
    __slots__ = ("transfer_id", "offset", "data", "last")
    TRANSFER_ID_FIELD_NUMBER: _ClassVar[int]
    OFFSET_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    LAST_FIELD_NUMBER: _ClassVar[int]
    transfer_id: str
    offset: int
    data: bytes
    last: bool
    def __init__(self, transfer_id: _Optional[str] = ..., offset: _Optional[int] = ..., data: _Optional[bytes] = ..., last: _Optional[bool] = ...) -> None: ...

class UpgradeStatus(_message.Message):
    __slots__ = ("transfer_id", "state", "reason", "resume_from")
    TRANSFER_ID_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    RESUME_FROM_FIELD_NUMBER: _ClassVar[int]
    transfer_id: str
    state: str
    reason: str
    resume_from: int
    def __init__(self, transfer_id: _Optional[str] = ..., state: _Optional[str] = ..., reason: _Optional[str] = ..., resume_from: _Optional[int] = ...) -> None: ...
