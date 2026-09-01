"""OTLP/gRPC 接收端（默认 :4317，LoongSuite 的默认协议与端口）。

关键设计
--------
    MessageToDict(proto_request)          # 默认 camelCase
产出的结构与 OTLP/HTTP JSON body **完全一致**，因此可以复用 app.otlp 的解析逻辑，
避免 gRPC / HTTP 两套解析代码分叉。
"""
import logging
from concurrent import futures
from typing import Callable

import grpc
from google.protobuf.json_format import MessageToDict
from opentelemetry.proto.collector.trace.v1 import (
    trace_service_pb2,
    trace_service_pb2_grpc,
)

logger = logging.getLogger(__name__)


class TraceServiceServicer(trace_service_pb2_grpc.TraceServiceServicer):
    def __init__(self, on_payload: Callable[[dict], int]):
        self._on_payload = on_payload

    def Export(self, request, context):  # noqa: N802 - gRPC 方法名由 proto 决定
        count = self._on_payload(MessageToDict(request))
        logger.debug("gRPC 接收 %s 个 span", count)
        return trace_service_pb2.ExportTraceServiceResponse()


def start_grpc_server(port: int, on_payload: Callable[[dict], int]) -> grpc.Server:
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
    trace_service_pb2_grpc.add_TraceServiceServicer_to_server(
        TraceServiceServicer(on_payload), server
    )
    server.add_insecure_port(f"[::]:{port}")
    server.start()
    logger.info("OTLP/gRPC 接收端已启动: 0.0.0.0:%s", port)
    return server
