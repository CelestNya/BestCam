# Start BestCamHost.exe elevated (MFCreateVirtualCamera requires admin token).
# Run this from an elevated terminal (admin). No UAC prompt appears there.
$hostPath = 'D:\Windows\Desktop\VR\Winx64_BestCam_v0.1\_internal\BestCamHost.exe'
Get-Process BestCamHost -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 500
Start-Process $hostPath -WorkingDirectory (Split-Path $hostPath)
Write-Output 'BestCamHost started.'
