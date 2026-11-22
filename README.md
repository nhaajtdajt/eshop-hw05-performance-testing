# EShop HW05 Performance Testing

JMeter plans for the EShop recovery, browse, checkout and cancel workflow. The repository includes Load, Stress and Spike plans and synthetic CSV fixtures.

## Run locally

1. Clone the [EShop system](https://github.com/ttbhanh/eshop-sut) into a sibling folder named `eshop-sut`.
2. Install Node.js, Python 3.12 or newer, JDK 17 or 21 and Apache JMeter 5.6.3. Use Windows PowerShell 5.1 or PowerShell 7.
3. Run `scripts/Run-All.ps1` from Windows PowerShell, with the Java and JMeter installation paths.

```powershell
& '.\scripts\Run-All.ps1' -JavaHome 'C:\path\to\jdk21' -JMeterHome 'C:\path\to\apache-jmeter-5.6.3'
```

The runner binds the test backend to loopback and seeds a fresh SQLite fixture for each main run. Result files are written to `results/` and the summary to `measurements.md`.
