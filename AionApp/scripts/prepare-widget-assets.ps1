param(
    [string]$SourceDir = '',
    [string]$OutputDir = (Join-Path $PSScriptRoot '..\app\src\main\res\drawable-nodpi')
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing

$publicRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\public'))
if ([string]::IsNullOrWhiteSpace($SourceDir)) {
    $sourcePath = Get-ChildItem -LiteralPath $publicRoot -Directory |
        Where-Object { (Get-ChildItem -LiteralPath $_.FullName -File -Filter 'Aion-*.png').Count -ge 2 } |
        Select-Object -First 1 -ExpandProperty FullName
    if (-not $sourcePath) {
        throw "Could not find the widget source directory under $publicRoot"
    }
}
else {
    $sourcePath = [System.IO.Path]::GetFullPath($SourceDir)
}
$outputPath = [System.IO.Path]::GetFullPath($OutputDir)
[System.IO.Directory]::CreateDirectory($outputPath) | Out-Null

function Export-TransparentCanvas {
    param(
        [string]$InputPath,
        [string]$OutputName,
        [int]$CanvasWidth,
        [int]$CanvasHeight,
        [int]$MaxWidth,
        [int]$MaxHeight,
        [ValidateSet('Left', 'Center', 'Right')][string]$HorizontalAnchor
    )

    if (-not (Test-Path -LiteralPath $InputPath)) {
        throw "Missing widget source image: $InputPath"
    }

    $source = [System.Drawing.Image]::FromFile($InputPath)
    $scale = [Math]::Min($MaxWidth / $source.Width, $MaxHeight / $source.Height)
    $width = [Math]::Max(1, [int][Math]::Round($source.Width * $scale))
    $height = [Math]::Max(1, [int][Math]::Round($source.Height * $scale))
    $x = switch ($HorizontalAnchor) {
        'Left' { 0 }
        'Right' { $CanvasWidth - $width }
        default { [int](($CanvasWidth - $width) / 2) }
    }
    $y = $CanvasHeight - $height

    $canvas = New-Object System.Drawing.Bitmap($CanvasWidth, $CanvasHeight, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $graphics = [System.Drawing.Graphics]::FromImage($canvas)
    try {
        $graphics.Clear([System.Drawing.Color]::Transparent)
        $graphics.CompositingMode = [System.Drawing.Drawing2D.CompositingMode]::SourceCopy
        $graphics.CompositingQuality = [System.Drawing.Drawing2D.CompositingQuality]::HighQuality
        $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
        $graphics.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
        $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality
        $graphics.DrawImage($source, $x, $y, $width, $height)
        $canvas.Save((Join-Path $outputPath $OutputName), [System.Drawing.Imaging.ImageFormat]::Png)
    }
    finally {
        $graphics.Dispose()
        $canvas.Dispose()
        $source.Dispose()
    }
}

function Find-RoleImage {
    param([string]$Prefix, [bool]$Tall)
    $matches = Get-ChildItem -LiteralPath $sourcePath -File -Filter "$Prefix-*.png" | ForEach-Object {
        $image = [System.Drawing.Image]::FromFile($_.FullName)
        try {
            [PSCustomObject]@{ Path = $_.FullName; Height = $image.Height }
        }
        finally {
            $image.Dispose()
        }
    }
    $selected = if ($Tall) {
        $matches | Sort-Object Height -Descending | Select-Object -First 1
    }
    else {
        $matches | Sort-Object Height | Select-Object -First 1
    }
    if (-not $selected) { throw "Missing source image for $Prefix" }
    return $selected.Path
}

$otherImages = Get-ChildItem -LiteralPath $sourcePath -File -Filter '*.png' |
    Where-Object { $_.BaseName -notlike 'Aion-*' -and $_.BaseName -notlike 'Connor-*' } |
    ForEach-Object {
        $image = [System.Drawing.Image]::FromFile($_.FullName)
        try {
            [PSCustomObject]@{ Path = $_.FullName; Ratio = $image.Width / $image.Height }
        }
        finally {
            $image.Dispose()
        }
    }
$microphonePath = $otherImages | Where-Object { $_.Ratio -lt 1.3 } |
    Select-Object -First 1 -ExpandProperty Path
$bannerPath = $otherImages | Where-Object { $_.Ratio -ge 1.3 } |
    Select-Object -First 1 -ExpandProperty Path
if (-not $microphonePath) { throw 'Missing microphone source image' }
if (-not $bannerPath) { throw 'Missing banner source image' }

Export-TransparentCanvas (Find-RoleImage 'Connor' $true) 'widget_companion_left_calm.png' 800 1200 800 1160 Left
Export-TransparentCanvas (Find-RoleImage 'Connor' $false) 'widget_companion_left_impatient.png' 800 1200 800 1160 Left
Export-TransparentCanvas (Find-RoleImage 'Aion' $true) 'widget_companion_right_calm.png' 800 1200 800 1160 Right
Export-TransparentCanvas (Find-RoleImage 'Aion' $false) 'widget_companion_right_impatient.png' 800 1200 800 1160 Right
Export-TransparentCanvas $microphonePath 'widget_microphone.png' 384 384 360 360 Center
Export-TransparentCanvas $bannerPath 'widget_banner.png' 1200 676 1200 676 Center

Write-Output "Widget assets prepared in $outputPath"
