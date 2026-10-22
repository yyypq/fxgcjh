# 串口测试工具

一个只依赖 [`pyserial`](https://pypi.org/project/pyserial/) 的命令行串口调试脚本，用来验证串口链路是否正常、设备用的是哪个波特率、以及实时查看设备吐出来的数据。

## 环境准备

需要 Python 3.8 及以上版本。

```bash
pip install -r requirements.txt
```

或者单独装：

```bash
pip install pyserial
```

## 四个子命令

### 1. `list` —— 看本机有哪些串口

```bash
python serial_test.py list
```

会列出端口名、描述、硬件 ID、VID:PID。选错端口之前先跑这个，最省事。

### 2. `loopback` —— 回环自测

需要**把模块的 TX 和 RX 用一根杜邦线短接**，发出去的数据会原样回到自己这边，脚本逐帧比对是否完全一致。

```bash
python serial_test.py loopback -p COM3 -b 115200 -n 200
```

| 参数 | 说明 |
| --- | --- |
| `-p/--port` | 串口名，`COM3` 或直接写 `3` |
| `-b/--baud` | 波特率，默认 115200 |
| `-n/--count` | 发多少帧，默认 100 |
| `-s/--size` | 每帧字节数，默认 256 |
| `--pattern` | `increment` 递增（默认）/ `fixed` 全 0x55 / `random` 随机 |
| `--frame-timeout` | 单帧回读超时，默认 0.5 秒 |

输出里会给出成功帧数、失败帧数、实测吞吐和理论吞吐。失败的帧会打印期望数据和实际数据的 HEX 对照，方便判断是丢字节、错位还是全乱。

退出码 0 = 全通过，1 = 有失败，可以直接串到批处理或 CI 里。

### 3. `scan` —— 多波特率自动扫描

不知道设备跑在哪个波特率时的救场工具。它会逐个尝试常见波特率，各发一帧探测数据，看谁有回应。

```bash
python serial_test.py scan -p COM3
```

只扫部分波特率、或者设备要先收到特定指令才吭声：

```bash
# 只测这几个
python serial_test.py scan -p COM3 --bauds 9600 19200 115200

# 发 AT 指令，期望回 OK
python serial_test.py scan -p COM3 --probe 'AT\r\n' --probe-format text --expect 'OK\r\n'
```

| 参数 | 说明 |
| --- | --- |
| `--bauds` | 自定义波特率列表，不给就扫常见值 |
| `--probe` | 探测数据，默认 `55 55 55 55 55 55 55 55` |
| `--probe-format` | `hex`（默认）或 `text` |
| `--expect` | 期望回应，给了就做精确比对 |
| `--idle` | 静默多久算设备说完，默认 0.15 秒 |

**原理和坑**：波特率不对时通常也能收到字节，只是全是乱码。所以脚本把"收到数据量"作为主要线索，最多的那个最可能是对的；如果给了 `--expect` 则额外判定是否精确命中。最终结论建议再用 `monitor` 人工看一眼。

### 4. `monitor` —— 实时收发打印

一边看设备输出，一边手动发指令，HEX 和 ASCII 对照显示，带毫秒时间戳。

```bash
# 只监听
python serial_test.py monitor -p COM3 -b 9600

# 先发一条 AT，然后继续监听，10 秒后自动结束
python serial_test.py monitor -p COM3 -b 9600 --send 'AT\r\n' --send-format text --duration 10

# 每 2 秒重发一次心跳
python serial_test.py monitor -p COM3 --send 'ping' --send-format text --repeat 2
```

`Ctrl+C` 退出，退出时会统计本次共收到多少字节。

## 串口参数

`loopback` / `scan` / `monitor` 都支持这些通用参数：

| 参数 | 说明 |
| --- | --- |
| `--bytesize` | 数据位，5/6/7/8，默认 8 |
| `--parity` | 校验位，`N` 无 / `E` 偶 / `O` 奇 / `M` 标记 / `S` 空格，默认 `N` |
| `--stopbits` | 停止位，1 / 1.5 / 2，默认 1 |
| `--timeout` | 读超时（秒），默认 1.0 |

## 常见问题

**打开串口失败 / 拒绝访问**
同一时刻一个串口只能被一个程序打开。串口助手、Arduino IDE、烧写工具、厂商上位机都可能占着，先全关掉。

**list 里看不到设备**
USB 转串口驱动没装（CH340、CP2102、FT232 各家驱动不同），或者线本身只能充电不能传数据。

**回环测试全失败，但设备通信是正常的**
回环测试必须物理短接 TX 和 RX。如果模块是 RS485，短接 A/B 无效，别硬套。

**scan 出来好几个波特率都有数据**
正常现象，乱码也会有字节。优先看 `--expect` 命中或数据量最大的那个，再用 `monitor` 确认现象是否合理。

## 目录结构

```
serial_test.py    主脚本（list / loopback / scan / monitor）
requirements.txt  依赖
README.md         本文档
```

## 许可

MIT
