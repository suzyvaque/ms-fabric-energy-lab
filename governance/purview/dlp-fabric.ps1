<#
Step 9 template: Purview DLP policy for Fabric / Power BI items (semantic models and lakehouses).
Sample, not production guidance. Run interactively by a Compliance/Purview admin:

  Install-Module ExchangeOnlineManagement -Scope CurrentUser
  Connect-IPPSSession                       # Security & Compliance PowerShell (interactive sign-in)
  ./dlp-fabric.ps1 -WorkspaceIds <ce-finance-dev id>,<ce-finance-prod id> -NotifyUser <admin UPN> [-Enforce]

Licensing: Microsoft 365 E5 / E5 Compliance (or Purview DLP add-on) for DLP on Fabric; Fabric capacity (F/P SKU).
Lakehouse support for DLP is in preview. Policy tips + alerts only unless -Enforce (restrict access for guests/externals).
#>
param(
    [Parameter(Mandatory)] [string[]] $WorkspaceIds,
    [Parameter(Mandatory)] [string] $NotifyUser,
    [switch] $Enforce
)
$ErrorActionPreference = "Stop"
$policy = "Contoso Energy - Fabric PII (lab)"

if (-not (Get-DlpCompliancePolicy -Identity $policy -ErrorAction SilentlyContinue)) {
    New-DlpCompliancePolicy -Name $policy -Mode Enable `
        -PowerBIDlpLocation $WorkspaceIds `
        -Comment "Detect customer PII (email, phone) in Fabric semantic models and lakehouses of the finance workspaces."
}

$rule = "PII in finance data"
$sits = @(
    @{ Name = "Email Address"; minCount = "5" },          # Purview built-in sensitive info types
    @{ Name = "U.S. Phone Number"; minCount = "5" }
)
$common = @{
    Policy                              = $policy
    ContentContainsSensitiveInformation = $sits
    GenerateIncidentReport              = $NotifyUser
    IncidentReportContent               = "All"
    NotifyUser                          = "Owner"
    NotifyPolicyTipCustomText           = "This item contains customer PII. Keep it labeled Confidential and restrict sharing."
    ReportSeverityLevel                 = "Medium"
}
if ($Enforce) { $common.RestrictAccess = @(@{ setting = "ExcludeContentProcessing"; value = "Block" }) }

if (Get-DlpComplianceRule -Identity $rule -ErrorAction SilentlyContinue) {
    Set-DlpComplianceRule -Identity $rule @common
} else {
    New-DlpComplianceRule -Name $rule @common
}
Get-DlpCompliancePolicy -Identity $policy | Format-List Name, Mode, PowerBIDlpLocation, DistributionStatus
