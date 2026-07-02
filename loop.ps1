param(
    [Parameter(ValueFromRemainingArguments=$true)]
    [string[]]$Arguments
)

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$pythonScript = "$scriptDir\agent_loop.py"

# Run Python and pass all arguments as-is
& python $pythonScript @Arguments