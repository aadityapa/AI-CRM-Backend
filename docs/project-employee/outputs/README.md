# Outputs

Automated Playwright/Puppeteer screenshots were **not** captured in this environment
(no headed browser session against `https://192.168.1.87:2020/admin` — see DESIGN-DECISIONS D14).

## Manual capture checklist

| File | What to capture |
|------|-----------------|
| `pe-list.png` | Project Employees list with filters, leave balance, PO chip, group-by |
| `pe-detail-general.png` | General tab |
| `pe-detail-leave.png` | Leave tab + apply leave |
| `pe-detail-holidays.png` | Holidays read-only calendar |
| `pe-detail-timesheet.png` | Timesheet rollups with W−L−H formula |
| `pe-detail-commercial.png` | Rates + PO drawdown bar |

Place PNGs in this folder after capture.
