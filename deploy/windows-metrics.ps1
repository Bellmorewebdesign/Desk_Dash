# Read-only OS counters for the Desk Dash Windows agent. Temperatures come from LHM data.json.
$ErrorActionPreference = 'Stop'
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
$osInfo = Get-CimInstance Win32_OperatingSystem
$disks = @(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | ForEach-Object {
    [pscustomobject]@{ mount = $_.DeviceID; source = $_.DeviceID; filesystem = $_.FileSystem;
        total_bytes = [long]$_.Size; free_bytes = [long]$_.FreeSpace; used_bytes = [long]($_.Size - $_.FreeSpace) }
})
$network = @{}
foreach ($nic in [System.Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces()) {
    if ($nic.OperationalStatus -ne 'Up' -or $nic.NetworkInterfaceType -eq 'Loopback') { continue }
    $stats = $nic.GetIPv4Statistics()
    $network[$nic.Name] = @{ rx = [long]$stats.BytesReceived; tx = [long]$stats.BytesSent }
}
$total = [long]$osInfo.TotalVisibleMemorySize * 1024
$free = [long]$osInfo.FreePhysicalMemory * 1024
$report = [pscustomobject]@{
    cpu_model = $cpu.Name; cpu_percent = [double]$cpu.LoadPercentage; cpu_mhz = [double]$cpu.CurrentClockSpeed;
    ram_total_bytes = $total; ram_free_bytes = $free;
    uptime_seconds = [long]((Get-Date) - $osInfo.LastBootUpTime).TotalSeconds;
    process_count = @(Get-Process).Count; filesystems = $disks; network = $network
}
$report | ConvertTo-Json -Depth 5 -Compress
