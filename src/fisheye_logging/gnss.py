from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading
import time
from typing import Any

from serial import Serial
from pyubx2 import (
    ERR_RAISE,
    POLL,
    SET,
    UBX_MSGIDS,
    UBX_PROTOCOL,
    UBXMessage,
    UBXReader,
)

from .config import GnssConfig

GPS_EPOCH = datetime(1980, 1, 6, tzinfo=timezone.utc)


class GnssError(RuntimeError):
    pass


def _jsonable_message(message: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"identity": message.identity}
    for key, value in vars(message).items():
        if key.startswith("_"):
            continue
        if isinstance(value, bytes):
            try:
                result[key] = value.rstrip(b"\x00").decode("ascii")
            except UnicodeDecodeError:
                result[key] = value.hex()
        elif isinstance(value, (str, int, float, bool)) or value is None:
            result[key] = value
        else:
            result[key] = str(value)
    return result


def _message_class_id(identity: str) -> tuple[int, int]:
    for key, name in UBX_MSGIDS.items():
        if name == identity:
            return key[0], key[1]
    raise GnssError(f"unknown UBX/CFG-MSG identity in configuration: {identity}")


class _TeeStream:
    """Tee bytes read from serial to the lossless observation trace."""

    def __init__(self, source: Serial, sink):
        self.source = source
        self.sink = sink

    def read(self, size: int = 1) -> bytes:
        data = self.source.read(size)
        if data:
            self.sink.write(data)
        return data


class GnssLogger:
    def __init__(self, config: GnssConfig, output_dir: Path):
        self.config = config
        self.output_dir = output_dir
        self.stream: Serial | None = None
        self.reader: UBXReader | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._error: BaseException | None = None
        self.actual: dict[str, Any] = {}

    def open_and_configure(self) -> dict[str, Any]:
        self.stream = Serial(
            self.config.port,
            self.config.baudrate,
            timeout=self.config.timeout_seconds,
        )
        self.reader = UBXReader(
            self.stream,
            protfilter=UBX_PROTOCOL,
            quitonerror=ERR_RAISE,
        )

        mon_ver = self._poll(UBXMessage("MON", "MON-VER", POLL), "MON-VER")
        current_tp5 = self._poll(
            UBXMessage(
                "CFG",
                "CFG-TP5",
                POLL,
                tpIdx=self.config.timepulse.index,
            ),
            "CFG-TP5",
        )

        self._send_set(
            UBXMessage(
                "CFG",
                "CFG-RATE",
                SET,
                measRate=self.config.measurement_rate_ms,
                navRate=self.config.nav_rate,
                timeRef=self.config.time_ref,
            )
        )

        tp = self.config.timepulse
        ratio = round(tp.duty_cycle * (1 << 32))
        self._send_set(
            UBXMessage(
                "CFG",
                "CFG-TP5",
                SET,
                tpIdx=tp.index,
                version=getattr(current_tp5, "version", 1),
                antCableDelay=getattr(current_tp5, "antCableDelay", 0),
                rfGroupDelay=getattr(current_tp5, "rfGroupDelay", 0),
                freqPeriod=tp.frequency_hz,
                freqPeriodLock=tp.frequency_hz,
                pulseLenRatio=ratio,
                pulseLenRatioLock=ratio,
                userConfigDelay=getattr(current_tp5, "userConfigDelay", 0),
                active=int(tp.active),
                lockGnssFreq=int(tp.lock_gnss_frequency),
                lockedOtherSet=0,
                isFreq=1,
                isLength=0,
                alignToTow=int(tp.align_to_tow),
                polarity=1 if tp.polarity == "rising" else 0,
                gridUtcGnss=0 if tp.time_grid == "utc" else 1,
                syncMode=getattr(current_tp5, "syncMode", 0),
            )
        )

        actual_messages: dict[str, dict[str, Any]] = {}
        for identity, rate_usb in self.config.messages.items():
            msg_class, msg_id = _message_class_id(identity)
            current = self._poll(
                UBXMessage(
                    "CFG",
                    "CFG-MSG",
                    POLL,
                    msgClass=msg_class,
                    msgID=msg_id,
                ),
                "CFG-MSG",
            )
            self._send_set(
                UBXMessage(
                    "CFG",
                    "CFG-MSG",
                    SET,
                    msgClass=msg_class,
                    msgID=msg_id,
                    rateDDC=int(getattr(current, "rateDDC", 0)),
                    rateUART1=int(getattr(current, "rateUART1", 0)),
                    rateUART2=int(getattr(current, "rateUART2", 0)),
                    rateUSB=rate_usb,
                    rateSPI=int(getattr(current, "rateSPI", 0)),
                )
            )
            observed = self._poll(
                UBXMessage(
                    "CFG",
                    "CFG-MSG",
                    POLL,
                    msgClass=msg_class,
                    msgID=msg_id,
                ),
                "CFG-MSG",
            )
            actual_messages[identity] = _jsonable_message(observed)

        actual_rate = self._poll(
            UBXMessage("CFG", "CFG-RATE", POLL),
            "CFG-RATE",
        )
        actual_tp5 = self._poll(
            UBXMessage("CFG", "CFG-TP5", POLL, tpIdx=tp.index),
            "CFG-TP5",
        )

        self.actual = {
            "mon_ver": _jsonable_message(mon_ver),
            "cfg_rate": _jsonable_message(actual_rate),
            "cfg_tp5": _jsonable_message(actual_tp5),
            "messages": actual_messages,
        }
        return self.actual

    def _poll(self, request, identity: str, timeout: float = 2.0):
        assert self.stream is not None and self.reader is not None
        self.stream.write(request.serialize())
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            _, message = self.reader.read()
            if message is None:
                continue
            if message.identity == identity:
                return message
        raise GnssError(f"timeout polling {identity}")

    def _send_set(self, request, timeout: float = 2.0) -> None:
        assert self.stream is not None and self.reader is not None
        self.stream.write(request.serialize())
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            _, message = self.reader.read()
            if message is None:
                continue
            if message.identity == "ACK-ACK":
                return
            if message.identity == "ACK-NAK":
                raise GnssError(f"receiver rejected {request.identity}")
        raise GnssError(f"timeout waiting for ACK to {request.identity}")

    def start(self) -> None:
        if self.stream is None:
            raise GnssError("GNSS serial port is not open")
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="gnss-logger",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        assert self.stream is not None
        ubx_path = self.output_dir / "gnss.ubx"
        csv_path = self.output_dir / "gnss.csv"
        try:
            with ubx_path.open("ab", buffering=0) as raw_out, csv_path.open(
                "w",
                newline="",
                encoding="utf-8",
            ) as csv_out:
                reader = UBXReader(
                    _TeeStream(self.stream, raw_out),
                    protfilter=UBX_PROTOCOL,
                    quitonerror=ERR_RAISE,
                )
                writer = csv.writer(csv_out)
                writer.writerow(
                    ["tp_utc", "q_err_ps", "tp_locked", "host_monotonic_ns"]
                )
                csv_out.flush()

                while not self._stop.is_set():
                    _, message = reader.read()
                    host_ns = time.monotonic_ns()
                    if message is None or message.identity != "TIM-TP":
                        continue

                    utc = ""
                    if (
                        int(getattr(message, "timeBase", 0)) == 1
                        and int(getattr(message, "utc", 0)) == 1
                    ):
                        target = GPS_EPOCH + timedelta(
                            weeks=int(message.week),
                            milliseconds=int(message.towMS),
                        )
                        utc = target.isoformat(timespec="milliseconds").replace(
                            "+00:00",
                            "Z",
                        )

                    q_err = ""
                    if int(getattr(message, "qErrInvalid", 1)) == 0:
                        q_err = int(message.qErr)

                    locked = int(getattr(message, "TpNotLocked", 1)) == 0
                    writer.writerow([utc, q_err, str(locked).lower(), host_ns])
                    csv_out.flush()
        except BaseException as exc:
            if not self._stop.is_set():
                self._error = exc

    def raise_if_failed(self) -> None:
        if self._error is not None:
            raise GnssError("GNSS logging failed") from self._error

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(
                timeout=max(1.0, self.config.timeout_seconds * 5)
            )
        if self.stream is not None:
            self.stream.close()
            self.stream = None
