#Requires -Version 5.1
<#
.SYNOPSIS
  Create a local code-signing certificate, trust it for this user, and sign ChatExportHub.exe.

.DESCRIPTION
  Installs a self-signed Code Signing certificate into:
    - CurrentUser\My              (private key)
    - CurrentUser\TrustedPublisher
    - CurrentUser\Root            (so Windows trusts the chain for this user)

  Then Authenticode-signs ChatExportHub.exe (Desktop + tools copies).

  Notes about 智能应用控制 / Smart App Control:
  - Signing removes "Unknown publisher" and helps classic SmartScreen.
  - Smart App Control in full Enforcement mode may STILL block self-signed
    apps without Microsoft cloud reputation. If the app is still blocked after
    signing, either switch SAC to Evaluation/Off, or run this script elevated
    to also install the cert machine-wide (LocalMachine stores).
#>
[CmdletBinding()]
param(
    [string]$Subject = 'CN=Mengz Local Code Signing, O=Local Dev, C=CN',
    [string]$FriendlyName = 'Mengz Local Code Signing (ChatExportHub)',
    [int]$YearsValid = 10,
    [string[]]$Targets = @(
        "$env:USERPROFILE\Desktop\ChatExportHub.exe",
        "$env:USERPROFILE\.grok\tools\ChatExportHub.exe",
        "$env:USERPROFILE\.grok\tools\dist\ChatExportHub.exe"
    ),
    [switch]$MachineWide  # requires Administrator
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$toolsDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$certDir = Join-Path $toolsDir 'certs'
$pfxPath = Join-Path $certDir 'mengz-local-codesign.pfx'
$cerPath = Join-Path $certDir 'mengz-local-codesign.cer'
$thumbPath = Join-Path $certDir 'thumbprint.txt'
# Fixed password only used for local PFX export on this machine (not a secret service).
$pfxPasswordPlain = 'ChatExportHub-Local-Only'
$pfxSecure = ConvertTo-SecureString -String $pfxPasswordPlain -Force -AsPlainText

function Test-IsAdmin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p = [Security.Principal.WindowsPrincipal]::new($id)
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-ExistingSigningCert {
    Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert -ErrorAction SilentlyContinue |
        Where-Object {
            $_.FriendlyName -eq $FriendlyName -or
            $_.Subject -eq $Subject -or
            $_.Subject -like 'CN=Mengz Local Code Signing*'
        } |
        Sort-Object NotAfter -Descending |
        Select-Object -First 1
}

function Install-CertToStore {
    param(
        [Parameter(Mandatory)][System.Security.Cryptography.X509Certificates.X509Certificate2]$Cert,
        [Parameter(Mandatory)][string]$StorePath  # e.g. Cert:\CurrentUser\Root
    )
    $storeLocation, $storeName = $StorePath -replace '^Cert:\\', '' -split '\\', 2
    $store = [System.Security.Cryptography.X509Certificates.X509Store]::new(
        $storeName,
        $storeLocation
    )
    $store.Open([System.Security.Cryptography.X509Certificates.OpenFlags]::ReadWrite)
    try {
        # Remove older same-subject certs to avoid clutter
        $existing = $store.Certificates | Where-Object { $_.Subject -eq $Cert.Subject }
        foreach ($old in $existing) {
            if ($old.Thumbprint -ne $Cert.Thumbprint) {
                $store.Remove($old)
            }
        }
        $store.Add($Cert)
    }
    finally {
        $store.Close()
    }
}

[void](New-Item -ItemType Directory -Path $certDir -Force)

$cert = Get-ExistingSigningCert
if (-not $cert) {
    Write-Host "Creating new code-signing certificate..."
    $cert = New-SelfSignedCertificate `
        -Type CodeSigningCert `
        -Subject $Subject `
        -FriendlyName $FriendlyName `
        -KeyAlgorithm RSA `
        -KeyLength 2048 `
        -HashAlgorithm SHA256 `
        -KeyExportPolicy Exportable `
        -KeySpec Signature `
        -CertStoreLocation 'Cert:\CurrentUser\My' `
        -NotAfter (Get-Date).AddYears($YearsValid) `
        -TextExtension @('2.5.29.37={text}1.3.6.1.5.5.7.3.3')
    Write-Host "Created: $($cert.Thumbprint)"
}
else {
    Write-Host "Reusing existing cert: $($cert.Thumbprint) (expires $($cert.NotAfter))"
}

# Export PFX (with private key) and CER (public)
Export-PfxCertificate -Cert $cert -FilePath $pfxPath -Password $pfxSecure | Out-Null
Export-Certificate -Cert $cert -FilePath $cerPath -Type CERT | Out-Null
Set-Content -Path $thumbPath -Value $cert.Thumbprint -Encoding ascii

# Public cert for trust stores (no private key)
$publicCert = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new($cerPath)

Write-Host "Installing into CurrentUser TrustedPublisher + Root..."
Install-CertToStore -Cert $publicCert -StorePath 'Cert:\CurrentUser\TrustedPublisher'
Install-CertToStore -Cert $publicCert -StorePath 'Cert:\CurrentUser\Root'

if ($MachineWide -or (Test-IsAdmin)) {
    Write-Host "Installing into LocalMachine TrustedPublisher + Root (admin)..."
    try {
        Install-CertToStore -Cert $publicCert -StorePath 'Cert:\LocalMachine\TrustedPublisher'
        Install-CertToStore -Cert $publicCert -StorePath 'Cert:\LocalMachine\Root'
        Write-Host "Machine-wide trust installed."
    }
    catch {
        Write-Warning "Machine-wide install failed (need Admin): $($_.Exception.Message)"
    }
}
else {
    Write-Host "Not elevated — skipped LocalMachine stores."
    Write-Host "To install machine-wide (stronger for SAC), re-run elevated:"
    Write-Host "  Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -MachineWide'"
}

# Sign targets
$timestampServers = @(
    'http://timestamp.digicert.com',
    'http://timestamp.sectigo.com',
    'http://timestamp.acs.microsoft.com'
)

$signed = 0
foreach ($target in $Targets) {
    if (-not (Test-Path -LiteralPath $target)) {
        Write-Host "Skip (missing): $target"
        continue
    }
    # Clear MOTW / zone mark
    Unblock-File -LiteralPath $target -ErrorAction SilentlyContinue

    $sig = $null
    $lastErr = $null
    foreach ($ts in $timestampServers) {
        try {
            $sig = Set-AuthenticodeSignature `
                -FilePath $target `
                -Certificate $cert `
                -TimestampServer $ts `
                -HashAlgorithm SHA256 `
                -Force
            if ($sig.Status -eq 'Valid' -or $sig.Status -eq 'UnknownError') {
                # UnknownError sometimes still has signature embedded if timestamp flaky
                break
            }
        }
        catch {
            $lastErr = $_
        }
    }
    if (-not $sig) {
        # Sign without timestamp as last resort
        $sig = Set-AuthenticodeSignature -FilePath $target -Certificate $cert -HashAlgorithm SHA256 -Force
    }

    $status = $sig.Status
    Write-Host ("Signed: {0}" -f $target)
    Write-Host ("  Status: {0}  Signer: {1}" -f $status, $sig.SignerCertificate.Subject)
    if ($status -ne 'Valid') {
        Write-Warning "Signature status is '$status' (may still run after trust install). $lastErr"
    }
    $signed++
}

Write-Host ""
Write-Host "==== Done ===="
Write-Host "Thumbprint : $($cert.Thumbprint)"
Write-Host "PFX        : $pfxPath"
Write-Host "CER        : $cerPath"
Write-Host "Files signed: $signed"
Write-Host ""
Write-Host "If 智能应用控制 still blocks the app:"
Write-Host "  1. Windows Security -> App & browser control -> Smart App Control"
Write-Host "     set to 'Evaluation' or 'Off' (Enforcement ignores most self-signed apps)."
Write-Host "  2. Or re-run this script as Administrator with -MachineWide."
Write-Host "  3. Then re-open ChatExportHub.exe from the Desktop."
