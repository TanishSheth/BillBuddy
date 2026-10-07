# Run the ShareSquad full regression suite and surface warnings.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

Write-Host "== ShareSquad full regression ==" -ForegroundColor Cyan
Write-Host "Working directory: $(Get-Location)"

$env:PYTHONWARNINGS = "default"
python -m pytest test_full_regression.py test_sharesquad.py test_monthly_reliability.py test_api_endpoints.py test_overpay_credit.py test_paid_private_bills.py test_quick_split.py -v --tb=short -W default 2>&1 | Tee-Object -FilePath "test_full_results.log"

$code = $LASTEXITCODE
Write-Host ""
Write-Host "Log written to test_full_results.log"
if ($code -ne 0) {
  Write-Host "FAILED with exit code $code" -ForegroundColor Red
  exit $code
}
Write-Host "ALL SUITES PASSED" -ForegroundColor Green
