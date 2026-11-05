try {
    Add-MpPreference -ExclusionPath 'D:\phpstudy_pro\WWW\FilmCms\public\static\images\film-cover' -ErrorAction Stop
    Write-Output 'OK-Defender exclusion added'
    $p = Get-MpPreference -ErrorAction SilentlyContinue
    Write-Output 'Current exclusion paths:'
    $p.ExclusionPath | ForEach-Object { Write-Output ("  " + $_) }
} catch {
    Write-Output ("FAIL: " + $_.Exception.Message)
}