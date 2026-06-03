param(
    [string]$InputArg
)

# This script is assumed to be in the same folder as the agent_relay.py script. We need to get the path to that folder to run the python script.
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition

# check if $InputArg is an existing file name:
if (-not (Test-Path $InputArg)) {

    # if $InputArg ends with .md:
    if (-not ($InputArg -match '\.md$')) {
        # Run create command and capture all output
        $output = & python "$scriptDir\agent_relay.py" --create $InputArg | Out-String
    } else {
        $output = & python "$scriptDir\agent_relay.py" --create "" $InputArg | Out-String
    }

    # Extract only the filename - we look for the last line that looks like a .md file path
    $lines = $output.Trim() -split "`n"

    # Find the line that contains the actual filename (usually the last .md path)
    $filenameLine = $lines | Where-Object { $_ -match '\.md$' } | Select-Object -Last 1

    # The filenameLine  contains the filename after the colon:
    $filename = $filenameLine -replace '.*: \s*', ''  # Remove everything before the colon and any whitespace

    if (-not $filename -or -not (Test-Path $filename)) {
        Write-Error "Failed to extract valid filename. Raw output was:"
        Write-Host $output -ForegroundColor Yellow
        exit 1
    }

    Write-Host "File created: $filename" -ForegroundColor Green

} else {
    $filename = $InputArg
    Write-Host "File already exists: $filename" -ForegroundColor Yellow
}

# Open in VS Code
code "$filename"

# Run the main command
Write-Host "Running agent..." -ForegroundColor Cyan

python "$scriptDir\agent_relay.py" "$filename"