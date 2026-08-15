# Stop BestCam processes and the FrameServer service so the driver DLL can be replaced.
taskkill /F /IM BestCam.exe 2>$null
taskkill /F /IM BestCamHost.exe 2>$null
Start-Sleep -Milliseconds 500
net stop FrameServer
