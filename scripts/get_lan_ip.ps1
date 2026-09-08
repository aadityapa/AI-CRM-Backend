$ErrorActionPreference = "SilentlyContinue"

function IsUsableIPv4([string]$ip) {
  if (-not $ip) { return $false }
  if ($ip -match '^(127\.|169\.254\.)') { return $false }
  if ($ip -eq '0.0.0.0') { return $false }
  return $ip -match '^(\d{1,3}\.){3}\d{1,3}$'
}

function IsPrivateIPv4([string]$ip) {
  return $ip -match '^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[0-1])\.)'
}

$ips = @()
foreach ($i in (Get-NetIPAddress -AddressFamily IPv4)) {
  $ip = $i.IPAddress
  if (IsUsableIPv4 $ip) { $ips += $ip }
}

$picked = $null
foreach ($ip in $ips) {
  if (IsPrivateIPv4 $ip) { $picked = $ip; break }
}
if (-not $picked -and $ips.Count -gt 0) { $picked = $ips[0] }

if ($picked) { Write-Output $picked }

