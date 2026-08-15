# BestCam driver upgrade (run elevated) - round 4: stop FrameServer to release DLL lock
$log = 'C:\Users\CelestNya\AppData\Local\Temp\bestcam_deploy.log'
Start-Transcript -Path $log -Force
$ErrorActionPreference = 'Continue'
Write-Output '=== deploy start (round 4) ==='
Get-Date
taskkill /F /IM BestCamHost.exe 2>&1 | Out-Null
Write-Output '--- stop FrameServer ---'
net stop FrameServer 2>&1 | Out-String | Write-Output
Start-Sleep -Seconds 2
Write-Output '--- copy DLL ---'
Copy-Item 'D:\Projects\2026-SummerHoliday\VRaio\BestCam\build\Release\BestCamSource.dll' 'D:\Windows\Desktop\VR\Winx64_BestCam_v0.1\_internal\BestCamSource.dll' -Force
Copy-Item 'D:\Projects\2026-SummerHoliday\VRaio\BestCam\build\Release\BestCamHost.exe' 'D:\Windows\Desktop\VR\Winx64_BestCam_v0.1\_internal\BestCamHost.exe' -Force
Get-Item 'D:\Windows\Desktop\VR\Winx64_BestCam_v0.1\_internal\BestCamSource.dll' | Select-Object Length, LastWriteTime | Out-String | Write-Output
Write-Output '--- regsvr32 ---'
regsvr32 /s 'D:\Windows\Desktop\VR\Winx64_BestCam_v0.1\_internal\BestCamSource.dll'
Write-Output "regsvr32 exit: $LASTEXITCODE"
Write-Output '--- start FrameServer ---'
net start FrameServer 2>&1 | Out-String | Write-Output
Write-Output '--- start host ---'
Start-Process 'D:\Windows\Desktop\VR\Winx64_BestCam_v0.1\_internal\BestCamHost.exe' -WorkingDirectory 'D:\Windows\Desktop\VR\Winx64_BestCam_v0.1\_internal'
Write-Output '--- done ---'
Stop-Transcript
