<#
FRX study host: create the Ubuntu Server VM on Hyper-V with fixed, recorded settings
(Blueprint v3.0, Section 14.1; G1 finding 2026-10-26: WSL2 cannot host Tetragon pod attribution).

Run in an ADMINISTRATOR PowerShell after Hyper-V is enabled and the PC has rebooted:
  Set-ExecutionPolicy -Scope Process Bypass
  .\hyperv_create_vm.ps1 -Iso C:\HyperV\<ubuntu-26.04.x-live-server-amd64.iso>

Settings and why:
  Generation 2, Secure Boot OFF   -> no kernel lockdown restricting eBPF (VM-only setting; Windows unaffected)
  12 GB STATIC memory             -> resources must not vary between runs (dynamic memory off)
  8 vCPUs                         -> same as the earlier qualified configuration
  Checkpoints DISABLED            -> nothing can silently roll back or freeze evidence on disk
  No automatic start              -> the VM runs only when you start it
#>
param(
  [Parameter(Mandatory = $true)][string]$Iso,
  [string]$Name = "frx-ubuntu",
  [string]$Dir = "C:\HyperV",
  [int64]$MemoryBytes = 12GB,
  [int]$Cpus = 8,
  [int64]$DiskBytes = 160GB
)
$ErrorActionPreference = "Stop"
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
      [Security.Principal.WindowsBuiltInRole]::Administrator)) { throw "Run this in an Administrator PowerShell." }
if (-not (Get-Command New-VM -ErrorAction SilentlyContinue)) { throw "Hyper-V PowerShell module not found: enable Hyper-V and reboot first." }
if (-not (Test-Path $Iso)) { throw "ISO not found: $Iso" }
if ($Iso -like "*OneDrive*") { throw "Copy the ISO out of OneDrive (e.g. to $Dir) first." }
if (Get-VM -Name $Name -ErrorAction SilentlyContinue) { throw "A VM named $Name already exists." }
if (-not (Get-VMSwitch -Name "Default Switch" -ErrorAction SilentlyContinue)) { throw "'Default Switch' not found." }

New-Item -ItemType Directory -Force -Path $Dir | Out-Null
$vhd = Join-Path $Dir "$Name.vhdx"
New-VM -Name $Name -Generation 2 -MemoryStartupBytes $MemoryBytes -NewVHDPath $vhd `
       -NewVHDSizeBytes $DiskBytes -SwitchName "Default Switch" -Path $Dir | Out-Null
Set-VMMemory    -VMName $Name -DynamicMemoryEnabled $false -StartupBytes $MemoryBytes
Set-VMProcessor -VMName $Name -Count $Cpus
Set-VM          -Name $Name -CheckpointType Disabled -AutomaticCheckpointsEnabled $false `
                -AutomaticStartAction Nothing -AutomaticStopAction ShutDown
Add-VMDvdDrive  -VMName $Name -Path $Iso
Set-VMFirmware  -VMName $Name -EnableSecureBoot Off -FirstBootDevice (Get-VMDvdDrive -VMName $Name)

# Record the exact configuration next to the VM (copy it into the evidence store later)
$rec = [ordered]@{
  created_utc   = (Get-Date).ToUniversalTime().ToString("o")
  windows       = (Get-ComputerInfo -Property WindowsProductName, WindowsEditionId, OsBuildNumber)
  vm            = (Get-VM -Name $Name | Select-Object Name, Generation, Version, ProcessorCount, MemoryStartup, DynamicMemoryEnabled, CheckpointType, AutomaticCheckpointsEnabled, AutomaticStartAction, AutomaticStopAction)
  firmware      = (Get-VMFirmware -VMName $Name | Select-Object SecureBoot, PreferredNetworkBootProtocol)
  disk          = (Get-VHD -Path $vhd | Select-Object Path, VhdType, Size)
  network       = (Get-VMNetworkAdapter -VMName $Name | Select-Object SwitchName, MacAddress, DynamicMacAddressEnabled)
  iso           = @{ path = $Iso; sha256 = (Get-FileHash -Algorithm SHA256 $Iso).Hash }
}
$out = Join-Path $Dir "$Name-config.json"
$rec | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 $out
Write-Host "VM $Name created. Configuration recorded in $out"
Write-Host "Start it with:  Start-VM $Name; vmconnect localhost $Name"
