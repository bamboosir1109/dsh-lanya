# Strict QQ reply protocol. This module performs no model, network or file I/O.
# A caller supplies exactly one model operation; only SPEAK carries sendable text.

function New-QqNoReplyDecision {
  param([string]$Reason, [string]$ErrorType = '')
  return [pscustomobject]@{
    Action = 'SILENT'; Text = ''; ImageTag = ''; Reason = $Reason; ErrorType = $ErrorType
  }
}

function ConvertFrom-QqReplyDecision {
  param([AllowNull()][AllowEmptyString()][string]$OutputText)

  if ([string]::IsNullOrWhiteSpace($OutputText)) {
    return (New-QqNoReplyDecision 'empty_output')
  }
  $answer = $OutputText.Trim().TrimStart([char]0xFEFF).Trim()
  if ($answer -ceq 'SILENT') { return (New-QqNoReplyDecision 'model_silent') }

  # Do not remove interior blank lines or accept an inline/partial SPEAK prefix.
  $rows = @([regex]::Split($answer, "\r\n|\n|\r"))
  if ($rows.Count -lt 2 -or $rows.Count -gt 3 -or $rows[0].Trim() -cne 'SPEAK') {
    return (New-QqNoReplyDecision 'invalid_protocol')
  }
  $text = $rows[1].Trim()
  if (-not $text -or $text -cmatch '^(SPEAK|SILENT|IMG\s+[a-z0-9_-]+)$' -or
      $text -match '[\x00-\x1f\x7f]') {
    return (New-QqNoReplyDecision 'invalid_reply')
  }
  $imageTag = ''
  if ($rows.Count -eq 3) {
    if ($rows[2] -cnotmatch '^\s*IMG\s+([a-z0-9_-]+)\s*$') {
      return (New-QqNoReplyDecision 'invalid_image_directive')
    }
    $imageTag = $Matches[1]
  }

  # Preserve the existing stage-direction guard, without manufacturing a reply.
  # Code points keep this file safe to dot-source in Windows PowerShell 5.1.
  $open = '[' + ([char]0xFF08) + '(' + '\[' + ([char]0x3010) + ']'
  $close = '[' + ([char]0xFF09) + ')' + '\]' + ([char]0x3011) + ']'
  if ($text -match ('^\s*' + $open + '.*' + $close + '\s*$')) {
    return (New-QqNoReplyDecision 'stage_direction')
  }
  return [pscustomobject]@{
    Action = 'SPEAK'; Text = $text; ImageTag = $imageTag; Reason = 'model_speak'; ErrorType = ''
  }
}

function Get-QqReplyDecision {
  param([string]$Task, [Parameter(Mandatory = $true)][scriptblock]$Ask)

  try {
    $answer = & $Ask $Task
    if ($null -ne $answer -and $answer -isnot [string]) {
      return (New-QqNoReplyDecision 'invalid_output_type')
    }
    return (ConvertFrom-QqReplyDecision $answer)
  }
  catch {
    # Keep diagnostics separate from anything the caller may deliver to QQ.
    return (New-QqNoReplyDecision 'ask_error' $_.Exception.GetType().Name)
  }
}
