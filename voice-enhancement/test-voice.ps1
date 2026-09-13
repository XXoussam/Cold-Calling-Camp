<#
.SYNOPSIS
    Run the cold-call agent in local console mode (mic/speakers) for a given
    campaign, to test voice quality without placing a real Twilio call.

.EXAMPLE
    ./voice-enhancement/test-voice.ps1 france
.EXAMPLE
    ./voice-enhancement/test-voice.ps1 us-friend -Record
#>
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("france", "us-friend")]
    [string]$Campaign,

    [switch]$Record
)

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $ProjectRoot
try {
    $env:CAMPAIGN = $Campaign
    if ($Record) {
        uv run python coldcall_agent.py console --record
    } else {
        uv run python coldcall_agent.py console
    }
} finally {
    Pop-Location
}
