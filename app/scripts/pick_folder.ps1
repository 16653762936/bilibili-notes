param(
    [string]$TitleB64 = "",
    [string]$OkLabelB64 = "",
    [string]$InitialB64 = ""
)

# Picks a folder and prints its absolute path to stdout (empty when cancelled).
#
# Two deliberate choices, both learned the hard way:
#
#  * This file is ASCII-only. Windows PowerShell 5.1 decodes a BOM-less script
#    with the ANSI code page, so Chinese literals here turn into mojibake.
#  * Localised text arrives base64-encoded (UTF-8) and is decoded below.
#    Passing Chinese straight through the command line gets mangled by the
#    console code page long before it reaches this script.
#
# Uses the Vista+ IFileDialog for the modern Explorer-style picker (address
# bar, breadcrumb, "New folder"). Falls back to the legacy FolderBrowserDialog
# only if the COM interop fails.

$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

function Decode([string]$b64, [string]$fallback) {
    if ([string]::IsNullOrWhiteSpace($b64)) { return $fallback }
    try { return [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($b64)) }
    catch { return $fallback }
}

$Title = Decode $TitleB64 "Select download folder"
$OkLabel = Decode $OkLabelB64 "Select this folder"
$InitialDirectory = Decode $InitialB64 ""

$src = @"
using System;
using System.Runtime.InteropServices;

public static class FolderPicker
{
    [DllImport("user32.dll")]
    private static extern IntPtr GetForegroundWindow();

    [ComImport, Guid("DC1C5A9C-E88A-4dde-A5A1-60F82A20AEF7")]
    private class FileOpenDialog { }

    [ComImport, Guid("42f85136-db7e-439c-85f1-e4075d135fc8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IFileDialog
    {
        [PreserveSig] int Show(IntPtr hwndOwner);
        void SetFileTypes(uint cFileTypes, IntPtr rgFilterSpec);
        void SetFileTypeIndex(uint iFileType);
        void GetFileTypeIndex(out uint piFileType);
        void Advise(IntPtr pfde, out uint pdwCookie);
        void Unadvise(uint dwCookie);
        void SetOptions(uint fos);
        void GetOptions(out uint pfos);
        void SetDefaultFolder(IntPtr psi);
        void SetFolder(IntPtr psi);
        void GetFolder(out IntPtr ppsi);
        void GetCurrentSelection(out IntPtr ppsi);
        void SetFileName([MarshalAs(UnmanagedType.LPWStr)] string pszName);
        void GetFileName([MarshalAs(UnmanagedType.LPWStr)] out string pszName);
        void SetTitle([MarshalAs(UnmanagedType.LPWStr)] string pszTitle);
        void SetOkButtonLabel([MarshalAs(UnmanagedType.LPWStr)] string pszText);
        void SetFileNameLabel([MarshalAs(UnmanagedType.LPWStr)] string pszLabel);
        void GetResult(out IShellItem ppsi);
        void AddPlace(IntPtr psi, int fdap);
        void SetDefaultExtension([MarshalAs(UnmanagedType.LPWStr)] string pszDefaultExtension);
        void Close(int hr);
        void SetClientGuid(ref Guid guid);
        void ClearClientData();
        void SetFilter(IntPtr pFilter);
    }

    [ComImport, Guid("43826d1e-e718-42ee-bc55-a1e261c37bfe"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IShellItem
    {
        void BindToHandler(IntPtr pbc, ref Guid bhid, ref Guid riid, out IntPtr ppv);
        void GetParent(out IShellItem ppsi);
        void GetDisplayName(uint sigdnName, [MarshalAs(UnmanagedType.LPWStr)] out string ppszName);
        void GetAttributes(uint sfgaoMask, out uint psfgaoAttribs);
        void Compare(IShellItem psi, uint hint, out int piOrder);
    }

    public static string Pick(string title, string okLabel)
    {
        IFileDialog dlg = (IFileDialog)(new FileOpenDialog());
        uint opts;
        dlg.GetOptions(out opts);
        // FOS_PICKFOLDERS(0x20) | FOS_FORCEFILESYSTEM(0x40) | FOS_PATHMUSTEXIST(0x800)
        dlg.SetOptions(opts | 0x20u | 0x40u | 0x800u);
        if (!string.IsNullOrEmpty(title)) dlg.SetTitle(title);
        if (!string.IsNullOrEmpty(okLabel)) dlg.SetOkButtonLabel(okLabel);
        // Own the dialog to the current foreground window, otherwise it can
        // open *behind* the app and look like the button did nothing.
        int hr = dlg.Show(GetForegroundWindow());
        if (hr != 0) return null;
        IShellItem item;
        dlg.GetResult(out item);
        string path;
        item.GetDisplayName(0x80058000u, out path);
        Marshal.ReleaseComObject(item);
        Marshal.ReleaseComObject(dlg);
        return path;
    }
}
"@

$picked = $null
try {
    if (-not ([System.Management.Automation.PSTypeName]'FolderPicker').Type) {
        Add-Type -TypeDefinition $src -Language CSharp | Out-Null
    }
    $picked = [FolderPicker]::Pick($Title, $OkLabel)
} catch {
    Add-Type -AssemblyName System.Windows.Forms | Out-Null
    $dlg = New-Object System.Windows.Forms.FolderBrowserDialog
    $dlg.Description = $Title
    $dlg.ShowNewFolderButton = $true
    if ($InitialDirectory -and (Test-Path -LiteralPath $InitialDirectory)) {
        $dlg.SelectedPath = $InitialDirectory
    }
    if ($dlg.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
        $picked = $dlg.SelectedPath
    }
    $dlg.Dispose()
}

if ($picked) { Write-Output $picked } else { Write-Output "" }
exit 0
