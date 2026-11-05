# 以 SYSTEM 权限写入 Defender 排除路径（由计划任务以最高权限调用）
$dir = 'D:\phpstudy_pro\WWW\FilmCms\public\static\images\film-cover'
$key = 'HKLM:\SOFTWARE\Microsoft\Windows Defender\Exclusions\Paths'
try {
    if (-not (Test-Path $key)) { New-Item -Path $key -Force | Out-Null }
    New-ItemProperty -Path $key -Name $dir -Value 0 -PropertyType DWord -Force -ErrorAction Stop | Out-Null
    Set-Content -Path 'D:\PythonProject\KPPACHONG-MOVIE\_defender_result.txt' -Value "OK: $dir" -Encoding UTF8
} catch {
    Set-Content -Path 'D:\PythonProject\KPPACHONG-MOVIE\_defender_result.txt' -Value ("FAIL: " + $_.Exception.Message) -Encoding UTF8
}
