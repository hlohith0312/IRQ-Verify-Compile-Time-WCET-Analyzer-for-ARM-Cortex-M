# irq-verify

**WCET Analyzer for ARM Cortex-M Interrupt Handlers**

Static analysis tool that verifies interrupt-disabled critical sections stay within cycle budgets on ARM Cortex-M microcontrollers.

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

---

## 🎯 What It Does

Prevents interrupt latency violations by analyzing worst-case execution time (WCET) of code between `__disable_irq()` and `__enable_irq()`.

**Problem:**
- Disabling interrupts too long causes deadline misses and system instability
- Manual cycle counting is error-prone
- Runtime testing misses worst-case paths

**Solution:**
- Automatic static analysis
- Detects budget violations before deployment
- Multiple analysis modes

---

## 📦 Installation

```bash
pip install git+https://github.com/YOUR-USERNAME/irq-verify.git
```

**Requirements:**
- Python 3.11+
- (Optional) arm-none-eabi-gcc for binary mode

---

## 🚀 Quick Start

### Step 1: Annotate Your Code

Add budget annotation above `__disable_irq()`:

```c
void uart_handler(void) {
    // @irq_budget(300)
    __disable_irq();
    
    uint8_t data = *UART_DR;
    process(data);
    
    __enable_irq();
}
```

### Step 2: Run Analysis

```bash
irq-verify src/interrupts.c --budget 300
```

### Step 3: Check Results

```
irq-verify -- src/interrupts.c
------------------------------------------------------------
Regions analysed: 1

  Region 1 -- uart_handler() line 42 (budget: 300 cycles)
  ✓ PASS 87 / 300 cycles

------------------------------------------------------------
✓ PASS All regions passed.
```

---

## 🎨 Analysis Modes

### C-AST Mode (Fast)
```bash
irq-verify firmware.c --budget 300
```
- Speed: ~100ms
- Accuracy: Conservative (±50-200%)
- No compiler needed

### Binary Mode (Accurate)
```bash
irq-verify firmware.c --budget 300 \
    --compile-with arm-none-eabi-gcc \
    --target cortex-m4 \
    --flash-wait-states 5
```
- Speed: ~2s
- Accuracy: +2-5% (never under-counts)
- Cycle-exact analysis

### Exact Measurement (100% Exact)
```bash
python -m irq_verify.exact_measurement firmware.c --function critical_section
```
- Accuracy: ±0 cycles
- Requires ARM board with DWT
- Hardware validation

---

## 📊 Output Formats

**Human-Readable (Default):**
```bash
irq-verify code.c --budget 300
```

**JSON (CI/CD):**
```bash
irq-verify code.c --budget 300 --output-format json
```

**SARIF (GitHub Security):**
```bash
irq-verify code.c --budget 300 --output-format sarif > results.sarif
```

---

## 🔧 Supported Architectures

- ARM Cortex-M0
- ARM Cortex-M0+
- ARM Cortex-M3
- ARM Cortex-M4
- ARM Cortex-M33
- ARM Cortex-M7

---

## 📝 Annotations

**Budget annotation** (required):
```c
// @irq_budget(N)
__disable_irq();
```

**Loop bound annotation** (for while/do-while loops):
```c
// @irq_loop_bound(10)
while (condition) {
    // loop body
}
```

---

## 🧪 Example

```c
#include <stdint.h>

volatile uint32_t *UART_DR = (volatile uint32_t *)0x40000000;

// This will PASS
void quick_handler(void) {
    // @irq_budget(100)
    __disable_irq();
    *UART_DR = 42;
    __enable_irq();
}

// This will FAIL
void slow_handler(void) {
    // @irq_budget(100)
    __disable_irq();
    for (int i = 0; i < 50; i++) {
        *UART_DR = i;
    }
    __enable_irq();
}
```

**Run analysis:**
```bash
$ irq-verify example.c --budget 100

irq-verify -- example.c
------------------------------------------------------------
Regions analysed: 2

  Region 1 -- quick_handler() (budget: 100 cycles)
  ✓ PASS 4 / 100 cycles

  Region 2 -- slow_handler() (budget: 100 cycles)
  ✗ FAIL 450 cycles > budget 100

------------------------------------------------------------
✗ FAIL 1 of 2 region(s) FAILED.
```

---

## 🛠️ Command-Line Options

```bash
irq-verify [files...] --budget N [options]

Analysis:
  --compile-with COMPILER      Enable binary mode (e.g., arm-none-eabi-gcc)
  --target ARCH                Target architecture (cortex-m4, cortex-m3, etc.)
  --flash-wait-states N        Flash wait states (0-15)

Output:
  --output-format FORMAT       text, json, or sarif
  --verbose                    Show detailed analysis

Other:
  --disable-fn NAME            Custom disable function name
  --enable-fn NAME             Custom enable function name
```

---

## ⚠️ Limitations

**Not supported:**
- Inline assembly
- Function pointers
- Recursion
- Unbounded loops without annotations

**Workarounds:**
- Use `@irq_loop_bound(N)` for while/do-while loops
- Inline external functions
- Avoid unsupported constructs in critical sections

---

## 🔍 How It Works

**C-AST Mode:**
1. Parses C source code
2. Builds control flow graph
3. Computes worst-case path

**Binary Mode:**
1. Compiles with arm-none-eabi-gcc
2. Disassembles ARM Thumb instructions
3. Analyzes pipeline hazards and wait states
4. Provides cycle-exact counts

**Exact Measurement:**
1. Instruments code with DWT cycle counter
2. Measures actual hardware execution
3. Reports exact cycle count (±0)

---

## 🎯 Use Cases

- Motor control (real-time deadlines)
- Communication protocols (UART, CAN, SPI)
- Sensor sampling
- Safety-critical systems
- DO-178C / IEC 61508 / ISO 26262 certification

---

## 🐛 Troubleshooting

**"Cannot find __disable_irq"**
```bash
irq-verify code.c --budget 300 \
    --disable-fn "DISABLE_INTERRUPTS" \
    --enable-fn "ENABLE_INTERRUPTS"
```

**"Unbounded loop detected"**
Add annotation:
```c
// @irq_loop_bound(10)
while (condition) { ... }
```

---

## 📄 License

Apache 2.0 - See [LICENSE](LICENSE) file.

---

**Built for embedded developers who need real-time guarantees.** 🚀
