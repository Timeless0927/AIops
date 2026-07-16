"""The sole canonical gate DAG and presentation-directory mapping."""

GATE_CONTRACT_REVISION = "pilot-clean-acceptance-v2"
PHASE_DIRECTORIES = (
    "00-package",
    "01-install",
    "02-setup",
    "03-recovery",
    "04-run-1",
    "05-run-2",
    "06-cleanup",
)
GATE_SEQUENCE = (
    "P01", "P02", "P03", "I01", "I02", "I03", "I04", "I05", "S01", "S02",
    "S03", "S04", "S05", "S06", "V01", "V02", "V03", "V04", "R05", "V05",
    "V06", "V07", "R01", "R02", "R03", "R04", "R06", "V08", "C01", "C02", "C03",
)
GATE_PHASE = {
    **{f"P{number:02d}": "00-package" for number in range(1, 4)},
    **{f"I{number:02d}": "01-install" for number in range(1, 6)},
    **{f"S{number:02d}": "02-setup" for number in range(1, 7)},
    **{f"R{number:02d}": "03-recovery" for number in range(1, 7)},
    **{f"V{number:02d}": "04-run-1" for number in range(1, 8)},
    "V08": "05-run-2",
    **{f"C{number:02d}": "06-cleanup" for number in range(1, 4)},
}
A01_GATE_SEQUENCE = GATE_SEQUENCE[: GATE_SEQUENCE.index("V01")]
