# The installer uses the same Windows typeface, navy surfaces and orange accent
# as the game's settings. Everything here also renders without showing a window.
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
Add-Type -ReferencedAssemblies System.Windows.Forms, System.Drawing -TypeDefinition @'
using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Runtime.InteropServices;
using System.Windows.Forms;
namespace B2Setup {
    public class Surface : Panel {
        public Surface() { DoubleBuffered = true; }
        public static GraphicsPath Round(RectangleF box, float radius) {
            var path = new GraphicsPath(); float diameter = radius * 2;
            path.AddArc(box.X, box.Y, diameter, diameter, 180, 90);
            path.AddArc(box.Right - diameter, box.Y, diameter, diameter, 270, 90);
            path.AddArc(box.Right - diameter, box.Bottom - diameter, diameter, diameter, 0, 90);
            path.AddArc(box.X, box.Bottom - diameter, diameter, diameter, 90, 90);
            path.CloseFigure(); return path;
        }
        protected override void OnPaintBackground(PaintEventArgs e) {
            e.Graphics.Clear(Parent == null ? BackColor : Parent.BackColor);
            e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
            using (var path = Round(new RectangleF(0.5f, 0.5f, Width - 1, Height - 1), 10))
            using (var brush = new SolidBrush(BackColor))
            using (var pen = new Pen(Color.FromArgb(49, 59, 71))) {
                e.Graphics.FillPath(brush, path); e.Graphics.DrawPath(pen, path);
            }
        }
        [DllImport("dwmapi.dll")]
        static extern int DwmSetWindowAttribute(IntPtr window, int attribute, ref int value, int size);
        public static void DarkTitleBar(IntPtr window) {
            int enabled = 1; DwmSetWindowAttribute(window, 20, ref enabled, 4);
        }
    }
    public class Progress : Control {
        int value;
        public Color Accent = Color.FromArgb(255, 126, 48);
        public int Value {
            get { return value; }
            set { this.value = Math.Max(0, Math.Min(1000, value)); Invalidate(); }
        }
        public Progress() { DoubleBuffered = true; AccessibleRole = AccessibleRole.ProgressBar; }
        protected override void OnPaint(PaintEventArgs e) {
            e.Graphics.Clear(BackColor); e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
            using (var track = Surface.Round(new RectangleF(0, 0, Width - 1, Height - 1), 5))
            using (var brush = new SolidBrush(Color.FromArgb(39, 47, 59))) e.Graphics.FillPath(brush, track);
            if (value == 0) return;
            float filled = (Width - 1) * value / 1000f;
            using (var fill = Surface.Round(new RectangleF(0, 0, filled, Height - 1), Math.Min(5, filled / 2)))
            using (var brush = new LinearGradientBrush(ClientRectangle, Accent, ControlPaint.Light(Accent), 0f))
                e.Graphics.FillPath(brush, fill);
        }
    }
}
'@

function New-SetupLabel($parent, [string]$text, [int]$x, [int]$y, [int]$width, [int]$height, [float]$size = 10, [bool]$bold = $false) {
    $label = [Windows.Forms.Label]::new()
    $label.Text = $text
    $label.SetBounds($x, $y, $width, $height)
    $weight = if ($bold) { [Drawing.FontStyle]::Bold } else { [Drawing.FontStyle]::Regular }
    $label.Font = [Drawing.Font]::new('Segoe UI', $size, $weight)
    $label.ForeColor = [Drawing.Color]::FromArgb(237, 240, 245)
    $parent.Controls.Add($label)
    return $label
}

function New-SetupButton([string]$text, [int]$x, [int]$width) {
    $button = [Windows.Forms.Button]::new()
    $button.Text = $text
    $button.SetBounds($x, 466, $width, 40)
    $button.FlatStyle = 'Flat'
    $button.FlatAppearance.BorderSize = 0
    $button.BackColor = [Drawing.Color]::FromArgb(41, 48, 59)
    $button.ForeColor = [Drawing.Color]::FromArgb(237, 240, 245)
    $button.Cursor = [Windows.Forms.Cursors]::Hand
    $button.UseVisualStyleBackColor = $false
    $form.Controls.Add($button)
    return $button
}

function Initialize-SetupWindow {
    [Windows.Forms.Application]::EnableVisualStyles()
    $script:form = [Windows.Forms.Form]::new()
    $form.Text = 'Burnout 2 - Installation'
    $form.AutoScaleDimensions = [Drawing.SizeF]::new(96, 96)
    $form.AutoScaleMode = 'Dpi'
    $form.ClientSize = [Drawing.Size]::new(820, 530)
    $form.StartPosition = 'CenterScreen'
    $form.FormBorderStyle = 'FixedDialog'
    $form.MaximizeBox = $false
    $form.Font = [Drawing.Font]::new('Segoe UI', 10)
    $form.BackColor = [Drawing.Color]::FromArgb(14, 17, 22)
    $form.Icon = [Drawing.Icon]::new((Join-Path $projectRoot 'resources\b2.ico'))
    $logo = [Windows.Forms.PictureBox]::new()
    $logo.SetBounds(24, 24, 60, 60)
    $logo.SizeMode = 'Zoom'
    $logo.Image = [Drawing.Image]::FromFile((Join-Path $projectRoot 'resources\b2.png'))
    $form.Controls.Add($logo)
    $form.Add_Disposed({ $logo.Image.Dispose() }.GetNewClosure())
    $null = New-SetupLabel $form 'INSTALLATION' 100 21 500 39 23 $true
    $subtitle = New-SetupLabel $form 'BURNOUT 2: POINT OF IMPACT' 102 65 500 22 9
    $subtitle.ForeColor = [Drawing.Color]::FromArgb(153, 166, 181)

    $rail = [B2Setup.Surface]::new()
    $rail.SetBounds(24, 110, 216, 286)
    $rail.BackColor = [Drawing.Color]::FromArgb(20, 24, 30)
    $form.Controls.Add($rail)
    $railTitle = New-SetupLabel $rail 'INSTALLATION' 18 18 180 22 9 $true
    $railTitle.ForeColor = [Drawing.Color]::FromArgb(153, 166, 181)
    $script:stageLabels = @()
    for ($i = 0; $i -lt $stageNames.Count; $i++) {
        $script:stageLabels += New-SetupLabel $rail ('{0:00}    {1}' -f ($i + 1), $stageNames[$i]) 18 (55 + $i * 35) 188 28 9.5
    }
    $card = [B2Setup.Surface]::new()
    $card.SetBounds(254, 110, 542, 286)
    $card.BackColor = [Drawing.Color]::FromArgb(20, 24, 30)
    $form.Controls.Add($card)
    $script:stageCaption = New-SetupLabel $card '' 24 22 490 24 9 $true
    $stageCaption.ForeColor = [Drawing.Color]::FromArgb(255, 139, 70)
    $script:stageTitle = New-SetupLabel $card '' 24 56 490 39 21 $true
    $script:status = New-SetupLabel $card '' 24 108 490 68 11
    $status.ForeColor = [Drawing.Color]::FromArgb(182, 194, 208)
    $script:progress = [B2Setup.Progress]::new()
    $progress.SetBounds(24, 190, 490, 12)
    $progress.BackColor = $card.BackColor
    $progress.AccessibleName = 'Current build task progress'
    $card.Controls.Add($progress)
    $script:taskCount = New-SetupLabel $card '' 24 220 490 44 10
    $taskCount.ForeColor = [Drawing.Color]::FromArgb(153, 166, 181)
    $script:logButton = New-SetupButton 'Open build log' 24 160
    $logButton.Add_Click({ Start-Process -FilePath 'notepad.exe' -ArgumentList ('"' + $logPath + '"') })
    $script:cancelButton = New-SetupButton 'Cancel' 198 110
    $cancelButton.Add_Click({
        if ($busy) { $script:cancelled = $true; $cancelButton.Enabled = $false; $cancelButton.Text = 'Cancelling...' }
        else { $form.Close() }
    })
    $script:openButton = New-SetupButton 'Open game folder' 596 200
    $openButton.Enabled = $false
    $openButton.Add_Click({ Start-Process -FilePath 'explorer.exe' -ArgumentList ('"' + $gameDirectory + '"') })
    $form.AcceptButton = $openButton
    $form.CancelButton = $cancelButton
    $form.Add_FormClosing({ param($sender, $eventArgs) if ($busy) { $script:cancelled = $true; $eventArgs.Cancel = $true } })
    [B2Setup.Surface]::DarkTitleBar($form.Handle)
}

function Update-SetupStage {
    $stageCaption.Text = 'STEP {0} OF {1}' -f $stage, $stageNames.Count
    $stageTitle.Text = $stageNames[$stage - 1]
    for ($i = 0; $i -lt $stageLabels.Count; $i++) {
        $stageLabels[$i].ForeColor = if ($i -eq $stage - 1) { [Drawing.Color]::FromArgb(255, 139, 70) }
            elseif ($i -lt $stage - 1) { [Drawing.Color]::FromArgb(237, 240, 245) }
            else { [Drawing.Color]::FromArgb(120, 134, 151) }
    }
}

function Complete-SetupWindow([bool]$succeeded) {
    $cancelButton.Enabled = $true
    $cancelButton.Text = 'Close'
    if ($succeeded) {
        $stageCaption.Text = 'ALL SIX STEPS COMPLETE'
        $stageTitle.Text = 'Ready to race.'
        $progress.Value = 1000
        $taskCount.Text = 'Installation complete. Your game is ready.'
        $openButton.BackColor = [Drawing.Color]::FromArgb(255, 126, 48)
        $openButton.ForeColor = [Drawing.Color]::FromArgb(20, 18, 15)
        $openButton.Enabled = $true
        $openButton.Focus() | Out-Null
        foreach ($label in $stageLabels) { $label.ForeColor = [Drawing.Color]::FromArgb(237, 240, 245) }
    } else {
        $stageTitle.Text = if ($cancelled) { 'Build cancelled' } else { 'Build needs attention' }
        $progress.Accent = [Drawing.Color]::FromArgb(240, 119, 104)
        $progress.Invalidate()
        $taskCount.Text = 'Completed work is kept. You can run the installer again.'
    }
}
