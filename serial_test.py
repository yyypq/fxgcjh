#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
串口测试工具 (serial_test.py)

一个不依赖除 pyserial 以外任何库的命令行串口调试脚本，提供四个子命令：

    list      列出本机所有串口
    loopback  回环自测（需要把 TX 和 RX 短接），校验收发数据是否完全一致
    scan      多波特率自动扫描，找出设备实际使用的波特率
    monitor   实时收发打印，HEX + ASCII 对照，适合盯着设备输出的场景

用法示例：
    python serial_test.py list
    python serial_test.py loopback -p COM3 -b 115200 -n 200
    python serial_test.py scan -p COM3
    python serial_test.py monitor -p COM3 -b 9600 --send "AT" --send-format text

退出码：0 表示测试通过，1 表示测试失败（方便接 CI 或批处理脚本）。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import unicodedata

try:
    import serial
    from serial.tools import list_ports
except ImportError:  # pragma: no cover
    sys.exit(
        "未找到 pyserial 库。请先安装：\n"
        "    pip install pyserial\n"
    )

__version__ = "1.0.0"

# 常见波特率，用于 scan 子命令的默认扫描列表
STANDARD_BAUDS = [
    1200, 2400, 4800, 9600, 14400, 19200, 38400, 57600,
    74880, 115200, 128000, 230400, 256000, 460800, 921600,
]

PARITY_MAP = {
    "N": serial.PARITY_NONE,
    "E": serial.PARITY_EVEN,
    "O": serial.PARITY_ODD,
    "M": serial.PARITY_MARK,
    "S": serial.PARITY_SPACE,
}

LINE = "-" * 62
THICK = "=" * 62


# --------------------------------------------------------------------------
# 通用小工具
# --------------------------------------------------------------------------

def parse_hex(text: str) -> bytes:
    """把 "55 AA 01"、"55aa01"、"0x55,0xAA" 之类的写法解析成 bytes。"""
    cleaned = text.replace(",", " ").replace("0x", " ").replace("0X", " ")
    out = bytearray()
    for token in cleaned.split():
        if len(token) % 2 != 0:
            raise ValueError(f"十六进制串长度必须是偶数：{token!r}")
        out.extend(bytes.fromhex(token))
    return bytes(out)


_ESCAPES = {"r": "\r", "n": "\n", "t": "\t", "0": "\0", "\\": "\\"}


def unescape_text(text: str) -> str:
    """
    处理 \\r \\n \\t \\0 \\\\ 和 \\xNN 这几种转义。
    自己扫一遍而不是用 unicode_escape，避免中文被当成 latin-1 编坏。
    """
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt in _ESCAPES:
                out.append(_ESCAPES[nxt])
                i += 2
                continue
            if nxt == "x" and i + 3 < len(text):
                try:
                    out.append(chr(int(text[i + 2:i + 4], 16)))
                    i += 4
                    continue
                except ValueError:
                    pass
        out.append(ch)
        i += 1
    return "".join(out)


def parse_payload(text: str, fmt: str) -> bytes:
    """按指定格式解析发送内容。"""
    if fmt == "text":
        return unescape_text(text).encode("utf-8")
    return parse_hex(text)


def hexdump(data: bytes, prefix: str = "", width: int = 16) -> str:
    """生成 HEX + ASCII 对照的多行文本。"""
    lines = []
    for offset in range(0, len(data), width):
        chunk = data[offset:offset + width]
        hex_part = " ".join(f"{b:02X}" for b in chunk).ljust(width * 3 - 1)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{prefix}{offset:04X}  {hex_part}  |{ascii_part}|")
    return "\n".join(lines)


def disp_width(text: str) -> int:
    """按终端显示宽度算长度：中日韩全角字符占 2 列。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def pad(text: str, width: int) -> str:
    """按显示宽度左对齐补空格，保证带中文的表格不会错位。"""
    return text + " " * max(0, width - disp_width(text))


def now_stamp() -> str:
    """毫秒级时间戳，给实时打印用。"""
    t = time.time()
    return time.strftime("%H:%M:%S", time.localtime(t)) + f".{int((t % 1) * 1000):03d}"


def normalize_port(port: str) -> str:
    """允许直接写端口号，比如 3 自动补成 COM3。"""
    port = str(port).strip()
    if port.isdigit():
        return f"COM{port}"
    return port


def open_port(args, timeout: float | None = None, baud: int | None = None,
              quiet: bool = False) -> serial.Serial:
    """
    按命令行参数打开串口。
    quiet=True 时失败不打印排查建议、直接抛出 SerialException，
    供 scan 这种要连续试十几次的场合用，免得刷屏。
    """
    try:
        ser = serial.Serial(
            port=normalize_port(args.port),
            baudrate=baud if baud is not None else args.baud,
            bytesize=args.bytesize,
            parity=PARITY_MAP[args.parity],
            stopbits=args.stopbits,
            timeout=args.timeout if timeout is None else timeout,
            write_timeout=2.0,
        )
    except serial.SerialException as exc:
        if quiet:
            raise
        sys.exit(
            f"打开串口失败：{exc}\n"
            f"排查建议：\n"
            f"  1. 用 'python serial_test.py list' 确认端口号是否正确；\n"
            f"  2. 关掉其他占用该串口的软件（串口助手、Arduino IDE、烧写工具等）；\n"
            f"  3. 检查 USB 转串口驱动是否装好。"
        )
    return ser


def read_exact(ser: serial.Serial, size: int, timeout: float) -> bytes:
    """尽力读满 size 个字节，超时则返回已读到的部分。"""
    buf = bytearray()
    deadline = time.perf_counter() + timeout
    while len(buf) < size:
        remain = deadline - time.perf_counter()
        if remain <= 0:
            break
        ser.timeout = min(remain, 0.05)
        chunk = ser.read(size - len(buf))
        if chunk:
            buf.extend(chunk)
    return bytes(buf)


def read_until_idle(ser: serial.Serial, idle: float, total_timeout: float,
                    max_bytes: int = 4096) -> bytes:
    """
    读到"数据流安静下来"为止（连续 idle 秒没有新数据），或到达总超时。
    比单纯读固定长度更适合扫描波特率——波特率不匹配时收到的乱码长度是不可预期的。
    """
    buf = bytearray()
    deadline = time.perf_counter() + total_timeout
    last_data_at = time.perf_counter()
    ser.timeout = 0.02
    while len(buf) < max_bytes:
        now = time.perf_counter()
        if now >= deadline:
            break
        chunk = ser.read(ser.in_waiting or 1)
        if chunk:
            buf.extend(chunk)
            last_data_at = time.perf_counter()
        elif time.perf_counter() - last_data_at >= idle:
            break
    return bytes(buf)


def serial_desc(args, baud: int | None = None) -> str:
    """拼一个 "COM3 @ 115200 8N1" 这样的描述串。"""
    baud = args.baud if baud is None else baud
    return f"{normalize_port(args.port)} @ {baud} {args.bytesize}{args.parity}{args.stopbits}"


# --------------------------------------------------------------------------
# 子命令：list
# --------------------------------------------------------------------------

def cmd_list(args) -> int:
    ports = sorted(list_ports.comports(), key=lambda p: p.device)
    if not ports:
        print("没有检测到任何串口设备。")
        print("如果是 USB 转串口模块，请检查线是否插好、驱动是否安装。")
        return 1

    print(THICK)
    print(f"检测到 {len(ports)} 个串口")
    print(THICK)
    for i, info in enumerate(ports, 1):
        vid_pid = ""
        if info.vid is not None and info.pid is not None:
            vid_pid = f"VID:PID={info.vid:04X}:{info.pid:04X}"
        print(f"[{i}] {info.device}")
        print(f"    描述    : {info.description or '-'}")
        print(f"    硬件 ID : {info.hwid or '-'}")
        if vid_pid:
            print(f"    识别码  : {vid_pid}")
        if info.manufacturer:
            print(f"    厂商    : {info.manufacturer}")
        if info.serial_number:
            print(f"    序列号  : {info.serial_number}")
        print(LINE)
    return 0


# --------------------------------------------------------------------------
# 子命令：loopback
# --------------------------------------------------------------------------

def make_payload(index: int, size: int, pattern: str) -> bytes:
    """
    生成第 index 帧的测试数据。
    increment：跨帧连续递增，能顺带发现"多字节/少字节"这类错位问题
    fixed    ：全 0x55，最经典的线路自测图样
    random   ：随机字节，覆盖性最好
    """
    if pattern == "increment":
        base = (index * size) % 256
        return bytes((base + i) % 256 for i in range(size))
    if pattern == "fixed":
        return b"\x55" * size
    return os.urandom(size)


def cmd_loopback(args) -> int:
    ser = open_port(args)
    ser.reset_input_buffer()
    ser.reset_output_buffer()

    print(THICK)
    print("回环自测（要求 TX 与 RX 物理短接，DTR/RTS 不要影响目标板）")
    print(THICK)
    print(f"串口      : {serial_desc(args)}")
    print(f"帧长/帧数 : {args.size} B x {args.count}")
    print(f"数据图样  : {args.pattern}")
    print(f"单帧超时  : {args.frame_timeout} s")
    print(LINE)

    failures: list[tuple[int, bytes, bytes]] = []
    total_bytes = 0
    done_frames = 0
    t0 = time.perf_counter()

    try:
        for index in range(args.count):
            payload = make_payload(index, args.size, args.pattern)
            try:
                ser.write(payload)
                ser.flush()
                total_bytes += len(payload)

                received = read_exact(ser, len(payload), args.frame_timeout)
                # 有多余字节时一起算进来，让差异更直观
                extra = ser.read(ser.in_waiting) if ser.in_waiting else b""
                received += extra
            except serial.SerialException as exc:
                print(f"\n第 {index} 帧通信异常，测试中止：{exc}")
                print("常见原因：USB 转串口被拔掉、驱动崩溃、线路短路。")
                break

            if received != payload:
                failures.append((index, payload, received))

            done_frames = index + 1
            if (args.count <= 20 or done_frames % max(1, args.count // 20) == 0
                    or done_frames == args.count):
                print(f"\r进度：{done_frames}/{args.count} 帧，失败 {len(failures)} 帧",
                      end="", flush=True)
    except KeyboardInterrupt:
        print("\n收到中断，提前结束测试。")
    finally:
        elapsed = time.perf_counter() - t0
        ser.close()
    print()

    ok_frames = done_frames - len(failures)
    print(LINE)
    print("回环测试结果")
    print(f"  串口      : {serial_desc(args)}")
    print(f"  总发送    : {total_bytes} B")
    print(f"  成功帧    : {ok_frames} / {done_frames}"
          + ("" if done_frames == args.count else f"（计划 {args.count} 帧，未跑完）"))
    print(f"  失败帧    : {len(failures)}")
    print(f"  耗时      : {elapsed:.3f} s")
    if elapsed > 0:
        actual_kbps = total_bytes * 8 / elapsed / 1000
        print(f"  实测吞吐  : {actual_kbps:.1f} kbit/s")
        print(f"  理论上限  : {args.baud / 1000:.1f} kbit/s（按 {args.baud} bps、10 bit/字节估算）")
    print(LINE)

    if failures:
        print(f"前 {min(len(failures), args.max_report)} 处失败明细（第 N 帧）：")
        for index, expected, got in failures[:args.max_report]:
            print(LINE)
            print(f"第 {index} 帧  期望 {len(expected)} B / 实际 {len(got)} B")
            print("期望数据：")
            print(hexdump(expected[:64], prefix="  "))
            print("实际收到：")
            print(hexdump(got[:64], prefix="  ") if got else "  （空，一个字节都没收到）")
        print(LINE)
        print("结论：失败")
        print("可能原因：TX/RX 没短接、波特率不匹配、线路接触不良、USB 转串口模块质量问题。")
        return 1

    print("结论：通过（收发数据完全一致）")
    return 0


# --------------------------------------------------------------------------
# 子命令：scan
# --------------------------------------------------------------------------

def cmd_scan(args) -> int:
    bauds = args.bauds or STANDARD_BAUDS
    probe = parse_payload(args.probe, args.probe_format)
    expect = parse_payload(args.expect, args.probe_format) if args.expect else None

    print(THICK)
    print("多波特率扫描")
    print(THICK)
    print(f"串口      : {normalize_port(args.port)}")
    print(f"探测数据  : {probe.hex(' ').upper()}  ({len(probe)} B)")
    if expect:
        print(f"期望回应  : {expect.hex(' ').upper()}")
    print(f"待测波特率: {', '.join(str(b) for b in bauds)}")
    print(f"每次等待  : 最长 {args.timeout} s，静默 {args.idle} s 视为结束")
    print(LINE)
    print(pad("波特率", 10) + pad("结果", 10) + pad("收到字节", 12) + "回应内容")
    print(LINE)

    hits: list[tuple[int, int, bytes]] = []
    open_errors = 0
    for baud in bauds:
        try:
            ser = open_port(args, timeout=args.idle, baud=baud, quiet=True)
        except serial.SerialException as exc:
            open_errors += 1
            if open_errors == 1:
                print(pad(str(baud), 10) + pad("打开失败", 10) + pad("-", 12)
                      + str(exc).split(":")[0])
            continue

        try:
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            ser.write(probe)
            ser.flush()
            data = read_until_idle(ser, idle=args.idle, total_timeout=args.timeout)
        except serial.SerialException as exc:
            print(pad(str(baud), 10) + pad("通信异常", 10) + pad("-", 12) + str(exc))
            continue
        finally:
            ser.close()

        if not data:
            print(pad(str(baud), 10) + pad("-", 10) + pad("0", 12))
            continue

        if expect is not None:
            passed = data.strip() == expect.strip()
            verdict = "命中" if passed else "有数据"
        else:
            passed = True
            verdict = "有数据"

        if passed:
            hits.append((baud, len(data), data))

        preview = data[:32].hex(" ").upper()
        if len(data) > 32:
            preview += "..."
        printable = "".join(chr(b) if 32 <= b < 127 else "." for b in data[:32])
        print(pad(str(baud), 10) + pad(verdict, 10) + pad(str(len(data)), 12)
              + f"{preview}  |{printable}|")

    print(LINE)
    if open_errors == len(bauds):
        print(f"结论：{normalize_port(args.port)} 根本无法打开，先检查端口号和占用情况。")
        print("用 'python serial_test.py list' 确认设备是否在线。")
        return 1
    if not hits:
        print("结论：所有波特率都没有收到数据。")
        print("可能原因：设备没上电 / 没在主动发送、TX-RX 接反、设备要求先发指令才回应、")
        print("          探测数据不对（试试 --probe 指定设备认识的指令，如 --probe-format text --probe \"AT\\r\\n\"）。")
        return 1

    # 收到数据最多的那个最可能是正确波特率
    hits.sort(key=lambda item: item[1], reverse=True)
    best_baud, best_len, best_data = hits[0]
    print("结论：以下波特率收到回应，按数据量排序（数据量最多的通常就是正确值）")
    for baud, length, data in hits:
        print(f"  {baud:<8} {length} B   {data[:24].hex(' ').upper()}")
    print(LINE)
    print(f"建议使用波特率：{best_baud}")
    if expect is not None and best_data.strip() != expect.strip():
        print("注意：收到数据与期望值不完全一致，可能是波特率接近但不完全正确，")
        print("      也可能是设备回的是乱码，请结合 monitor 子命令人工确认。")
    return 0


# --------------------------------------------------------------------------
# 子命令：monitor
# --------------------------------------------------------------------------

def cmd_monitor(args) -> int:
    if args.repeat and not args.send:
        print("参数错误：--repeat 需要配合 --send 一起用（没有要重发的内容）。")
        return 1

    ser = open_port(args)
    print(THICK)
    print("实时收发监控（Ctrl+C 退出）")
    print(THICK)
    print(f"串口      : {serial_desc(args)}")
    if args.duration:
        print(f"运行时长  : {args.duration} s 后自动结束")
    print(LINE)

    payload = b""
    if args.send:
        payload = parse_payload(args.send, args.send_format)
        ser.write(payload)
        ser.flush()
        print(f"[{now_stamp()}] 发送 {len(payload)} B")
        print(hexdump(payload, prefix="  "))

    stop_at = time.time() + args.duration if args.duration else None
    total_rx = 0
    last_send = time.time()

    try:
        while True:
            if stop_at and time.time() >= stop_at:
                print(LINE)
                print("已到设定时长，结束。")
                break

            if args.repeat and time.time() - last_send >= args.repeat:
                ser.write(payload)
                ser.flush()
                last_send = time.time()
                print(f"[{now_stamp()}] 定时重发 {len(payload)} B")

            chunk = ser.read(ser.in_waiting or 1)
            if chunk:
                total_rx += len(chunk)
                print(f"[{now_stamp()}] 接收 {len(chunk)} B")
                print(hexdump(chunk, prefix="  "))
    except KeyboardInterrupt:
        print()
        print("已手动停止。")

    ser.close()
    print(LINE)
    print(f"本次共接收 {total_rx} B")
    return 0


# --------------------------------------------------------------------------
# 命令行入口
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    # 串口通用参数，所有子命令共用
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-p", "--port", required=True,
                        help="串口名，如 COM3（Windows）或 /dev/ttyUSB0（Linux/macOS），也可只写 3")
    common.add_argument("-b", "--baud", type=int, default=115200,
                        help="波特率，默认 115200")
    common.add_argument("--bytesize", type=int, default=8, choices=[5, 6, 7, 8],
                        help="数据位，默认 8")
    common.add_argument("--parity", default="N", choices=list(PARITY_MAP),
                        help="校验位：N 无 / E 偶 / O 奇 / M 标记 / S 空格，默认 N")
    common.add_argument("--stopbits", type=float, default=1, choices=[1, 1.5, 2],
                        help="停止位，默认 1")
    common.add_argument("--timeout", type=float, default=1.0,
                        help="读超时（秒），默认 1.0")

    parser = argparse.ArgumentParser(
        prog="serial_test.py",
        description="串口测试工具：列出串口、回环自测、波特率扫描、实时收发监控。",
        epilog="示例：\n"
               "  python serial_test.py list\n"
               "  python serial_test.py loopback -p COM3 -b 115200 -n 200\n"
               "  python serial_test.py scan -p COM3\n"
               "  python serial_test.py monitor -p COM3 -b 9600 --send 'AT\\r\\n' --send-format text",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-v", "--version", action="version",
                        version=f"serial_test.py {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="子命令")

    p_list = sub.add_parser("list", help="列出本机所有串口")
    p_list.set_defaults(func=cmd_list)

    p_loop = sub.add_parser("loopback", parents=[common],
                            help="回环自测（需短接 TX/RX）")
    p_loop.add_argument("-n", "--count", type=int, default=100,
                        help="发送帧数，默认 100")
    p_loop.add_argument("-s", "--size", type=int, default=256,
                        help="每帧字节数，默认 256")
    p_loop.add_argument("--pattern", default="increment",
                        choices=["increment", "fixed", "random"],
                        help="数据图样：increment 递增 / fixed 全 0x55 / random 随机，默认 increment")
    p_loop.add_argument("--frame-timeout", type=float, default=0.5,
                        help="单帧回读超时（秒），默认 0.5")
    p_loop.add_argument("--max-report", type=int, default=3,
                        help="最多打印几处失败明细，默认 3")
    p_loop.set_defaults(func=cmd_loopback)

    p_scan = sub.add_parser("scan", parents=[common],
                            help="多波特率自动扫描")
    p_scan.add_argument("--bauds", type=int, nargs="+", default=None,
                        help="自定义待测波特率列表，默认扫常见值")
    p_scan.add_argument("--probe", default="55 55 55 55 55 55 55 55",
                        help="探测数据，默认 8 个 0x55")
    p_scan.add_argument("--probe-format", default="hex", choices=["hex", "text"],
                        help="探测数据格式，hex 或 text，默认 hex")
    p_scan.add_argument("--expect", default=None,
                        help="期望收到的回应（格式同 --probe-format），不给则只看是否收到数据")
    p_scan.add_argument("--idle", type=float, default=0.15,
                        help="静默多久认为设备停发（秒），默认 0.15")
    p_scan.set_defaults(func=cmd_scan)

    p_mon = sub.add_parser("monitor", parents=[common],
                           help="实时收发打印")
    p_mon.add_argument("--send", default=None,
                       help="启动时发送的数据，如 'AT\\r\\n'")
    p_mon.add_argument("--send-format", default="hex", choices=["hex", "text"],
                       help="发送数据格式，默认 hex")
    p_mon.add_argument("--repeat", type=float, default=None,
                       help="每隔多少秒重发一次（秒），默认不重发")
    p_mon.add_argument("--duration", type=float, default=None,
                       help="运行多少秒后自动结束，默认一直运行")
    p_mon.set_defaults(func=cmd_monitor)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except ValueError as exc:
        print(f"参数错误：{exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
