<#
.SYNOPSIS
Finds public Relocue job pages and checks their original employer links.

.EXAMPLE
powershell -File .\automation\scan-relocue.ps1 -SeedUrl 'https://relocue.com/jobs/example'

.EXAMPLE
powershell -File .\automation\scan-relocue.ps1 -SeedUrl 'https://relocue.com/jobs/example' -Format Json
#>
param(
    [ValidateSet('Table', 'Json', 'Csv')]
    [string]$Format = 'Table',
    [string[]]$SeedUrl = @()
)

$ErrorActionPreference = 'Stop'
$catalogPath = Join-Path (Split-Path -Parent $PSScriptRoot) '.local\relocue-urls.txt'
$userAgent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) RelocuePublicScanner/1.0'

function Invoke-CurlText {
    param([Parameter(Mandatory)][string]$Url)

    $lines = & curl.exe -sS -L --max-time 30 -A $userAgent $Url 2>$null
    return ($lines -join "`n")
}

function Get-UrlProbe {
    param([Parameter(Mandatory)][string]$Url)

    $probe = & curl.exe -sS -L --max-time 30 -A $userAgent `
        -o NUL -w "%{http_code}`t%{url_effective}" $Url 2>$null
    $parts = (($probe | Out-String).Trim()) -split "`t", 2
    return [pscustomobject]@{
        HttpStatus = $parts[0]
        EffectiveUrl = if ($parts.Count -gt 1) { $parts[1] } else { $Url }
    }
}

function ConvertTo-VisibleText {
    param([Parameter(Mandatory)][string]$Html)

    $text = $Html `
        -replace '(?is)<script.*?</script>', ' ' `
        -replace '(?is)<style.*?</style>', ' ' `
        -replace '<[^>]+>', ' ' `
        -replace '\s+', ' '
    return [System.Net.WebUtility]::HtmlDecode($text).Trim()
}

function Read-RelocueJob {
    param(
        [Parameter(Mandatory)][string]$Url,
        [string]$DiscoveredFrom
    )

    $html = Invoke-CurlText -Url $Url
    $jsonMatch = [regex]::Match(
        $html,
        '<script type="application/ld\+json">(.*?)</script>',
        'Singleline'
    )
    if (-not $jsonMatch.Success) {
        return [pscustomobject]@{
            Company = $null
            Title = $null
            Country = $null
            Added = $null
            LastVerified = $null
            EmployerStatus = 'missing_json_ld'
            RelocueUrl = $Url
            EmployerUrl = $null
            DiscoveredFrom = $DiscoveredFrom
        }
    }

    $job = [System.Net.WebUtility]::HtmlDecode($jsonMatch.Groups[1].Value) |
        ConvertFrom-Json
    $visible = ConvertTo-VisibleText -Html $html
    $added = [regex]::Match(
        $visible,
        'Added\s+([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})'
    ).Groups[1].Value
    $verified = [regex]::Match(
        $visible,
        'Last verified\s+([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})'
    ).Groups[1].Value

    if ($job.url -notmatch '^https://[A-Za-z0-9.-]+(?:/|$)') {
        return [pscustomobject]@{
            Title = $job.title
            RelocueUrl = $Url
            EmployerStatus = 'missing_or_invalid_employer_url'
        }
    }
    $employerProbe = Get-UrlProbe -Url $job.url
    $employerHtml = Invoke-CurlText -Url $job.url
    $employerText = ConvertTo-VisibleText -Html $employerHtml
    $closedSignal = $employerText -match '(?i)no longer accepting applications|position_not_found|job (?:is )?no longer available|job not found|vacancy (?:is )?closed'
    $employerStatus = if ($employerProbe.HttpStatus -notmatch '^[23]') {
        "http_$($employerProbe.HttpStatus)"
    } elseif ($closedSignal) {
        'likely_closed'
    } else {
        'reachable_no_closed_signal'
    }

    return [pscustomobject]@{
        Company = $job.hiringOrganization.name
        Title = $job.title
        Country = $job.jobLocation.address.addressCountry
        Added = if ($added) { $added } else { $null }
        LastVerified = if ($verified) { $verified } else { $null }
        EmployerStatus = $employerStatus
        RelocueUrl = $Url
        EmployerUrl = $job.url
        DiscoveredFrom = $DiscoveredFrom
    }
}

$discovered = @()

$catalogUrls = if (Test-Path -LiteralPath $catalogPath) {
    Get-Content -LiteralPath $catalogPath |
        Where-Object { $_ -match '^https://relocue\.com/jobs/' }
} else {
    @()
}

foreach ($url in @($catalogUrls) + @($SeedUrl)) {
    if ($url -match '^https://relocue\.com/jobs/[^/?#]+/?(?:[?#].*)?$') {
        $discovered += [pscustomobject]@{
            Url = ($url -replace '[?#].*$', '')
            From = if ($catalogUrls -contains $url) { 'catalog' } else { 'seed' }
        }
    }
}

$jobs = $discovered |
    Group-Object -Property Url |
    ForEach-Object {
        $sources = $_.Group.From | Sort-Object -Unique
        Read-RelocueJob -Url $_.Name -DiscoveredFrom ($sources -join ', ')
    } |
    Sort-Object -Property @{
        Expression = {
            if ($_.Added) {
                [datetime]::ParseExact(
                    $_.Added,
                    'MMM d, yyyy',
                    [System.Globalization.CultureInfo]::InvariantCulture
                )
            } else {
                [datetime]::MinValue
            }
        }
        Descending = $true
    }

switch ($Format) {
    'Json' { $jobs | ConvertTo-Json -Depth 6 }
    'Csv' { $jobs | ConvertTo-Csv -NoTypeInformation }
    default {
        $jobs | Format-Table Company, Title, Country, Added, LastVerified, EmployerStatus -AutoSize
        Write-Output "`nFound $($jobs.Count) Relocue job page(s)."
    }
}
