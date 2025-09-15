// Program.cs
// .NET 8 console app built as a DLL (UseAppHost=false)
// Hardcoded paths: change these 3 constants to your environment.
// Drop this DLL into the same folder as the Atalasoft 11.5 dependency DLLs + license files.
// Run with: dotnet OcrBatchDemo.dll

using System;
using System.IO;
using System.Linq;
using System.Diagnostics;
using System.Globalization;
using System.Collections.Generic;

using Atalasoft.Imaging;
using Atalasoft.Imaging.Codec;
using Atalasoft.Imaging.Codec.Pdf;
using Atalasoft.Imaging.ImageProcessing.Document; // AdvancedDocClean filters
using Atalasoft.Ocr;
using Atalasoft.Ocr.OmniPage;

internal static class Program
{
    // ***** EDIT THESE *****
    private const string INPUT_DIR  = @"D:\ocr\in";
    private const string OUTPUT_DIR = @"D:\ocr\out";
    private const string OMNIPAGE_RESOURCES = @"D:\ocr\omnipage_resources";
    private const string OCR_LANGUAGE = "en-US";   // e.g., "en-US", "es-ES", etc.
    private const int    PDF_RASTER_DPI = 300;     // good default for OCR

    private const bool   EMIT_LAYOUT_JSON = true;  // requires WebControls OCR translator to be present

    private static int Main(string[] args)
    {
        // Ensure folders
        if (!Directory.Exists(INPUT_DIR))  { Console.Error.WriteLine($"Input missing: {INPUT_DIR}"); return 2; }
        Directory.CreateDirectory(OUTPUT_DIR);
        if (!Directory.Exists(OMNIPAGE_RESOURCES)) { Console.Error.WriteLine($"OmniPage resources missing: {OMNIPAGE_RESOURCES}"); return 3; }

        // Register PdfDecoder at a good DPI for OCR
        EnsurePdfDecoderWithDpi(PDF_RASTER_DPI); // recommended to explicitly set DPI for consistent OCR input.  [oai_citation:1‡Atalasoft](https://www.atalasoft.com/kb2/KB/50067/HOWTO-Safely-Change-Set-Resolution-of-PdfDecoder?utm_source=chatgpt.com)

        var pdfs = Directory.EnumerateFiles(INPUT_DIR, "*.pdf", SearchOption.TopDirectoryOnly)
                            .OrderBy(p => p, StringComparer.OrdinalIgnoreCase)
                            .ToList();

        if (pdfs.Count == 0) { Console.WriteLine("No PDFs found."); return 0; }

        Console.WriteLine($"Input : {INPUT_DIR}");
        Console.WriteLine($"Output: {OUTPUT_DIR}");
        Console.WriteLine($"Omni  : {OMNIPAGE_RESOURCES}");
        Console.WriteLine($"Lang  : {OCR_LANGUAGE}");
        Console.WriteLine($"DPI   : {PDF_RASTER_DPI}");
        Console.WriteLine();

        foreach (var pdf in pdfs)
        {
            RunJobFor(pdf);
        }

        Console.WriteLine("\nDone.");
        return 0;
    }

    private static void RunJobFor(string pdfPath)
    {
        var name   = Path.GetFileNameWithoutExtension(pdfPath);
        var outDir = Path.Combine(OUTPUT_DIR, name);
        Directory.CreateDirectory(outDir);

        var logPath = Path.Combine(outDir, "ocr_metrics.log");
        using var log = new StreamWriter(logPath, append: false);

        Log(log, $"== OCR Job: {name} ==");
        Log(log, $"Started   : {DateTimeOffset.Now:O}");
        Log(log, $"Input PDF : {pdfPath}");
        Log(log, $"Output Dir: {outDir}");
        Log(log, "");

        var overall = Stopwatch.StartNew();

        // 1) Preprocess -> multipage TIFF in the output folder (easy to inspect)
        var swPre = Stopwatch.StartNew();
        var tiffForOcr = Path.Combine(outDir, "_preprocessed.tif");
        int pageCount  = BuildPreprocessedTiffFromPdf(pdfPath, tiffForOcr);
        swPre.Stop();
        Log(log, $"Preprocess: {swPre.Elapsed.TotalMilliseconds:n0} ms (pages: {pageCount})");

        // 2) OCR with OmniPage (resources path required) and produce artifacts
        var searchablePdf = Path.Combine(outDir, $"{name}.searchable.pdf");
        var plaintext     = Path.Combine(outDir, $"{name}.txt");
        var layoutJson    = Path.Combine(outDir, $"{name}.layout.json");

        using var loader = new OmniPageLoader(OMNIPAGE_RESOURCES);   // loads engine resources from the folder you provide.  [oai_citation:2‡Atalasoft](https://www.atalasoft.com/kb2/KB/50396/INFO-OmniPageEngine-Overview?utm_source=chatgpt.com)
        using var engine = new OmniPageEngine {
            RecognitionCultures = new[] { new CultureInfo(OCR_LANGUAGE) }
        };
        using var images = new FileSystemImageSource(new[] { tiffForOcr }, true); // multipage source.  [oai_citation:3‡Atalasoft](https://www.atalasoft.com/kb2/Print50279.aspx?utm_source=chatgpt.com)

        // 2a) Searchable PDF (image-under-text)
        var perPage = new List<(int page, double ms)>();
        var swPdf = Stopwatch.StartNew();
        using (var pdfTranslator = new PdfTranslator()) // “Use this translator to create searchable PDFs”.  [oai_citation:4‡DocShield](https://docshield.tungstenautomation.com/AtalasoftDotImage/en_US/11.5.0-8wax4k031j/help/DotImage/html/T_Atalasoft_Ocr_PdfTranslator.htm?utm_source=chatgpt.com)
        {
            var pageTimer = new Stopwatch();
            int current = -1;
            pdfTranslator.PageConstructing += (s, e) => {
                if (pageTimer.IsRunning) { pageTimer.Stop(); perPage.Add((current+1, pageTimer.Elapsed.TotalMilliseconds)); }
                current = e.PageIndex;
                pageTimer.Restart();
            };
            engine.Translate(images, "application/pdf", searchablePdf, pdfTranslator);
            if (pageTimer.IsRunning) { pageTimer.Stop(); perPage.Add((current+1, pageTimer.Elapsed.TotalMilliseconds)); }
        }
        swPdf.Stop();
        Log(log, $"OCR->PDF : {swPdf.Elapsed.TotalMilliseconds:n0} ms");

        // 2b) Plain text
        var swTxt = Stopwatch.StartNew();
        using (var textTranslator = new TextTranslator()) // plain-text output translator.  [oai_citation:5‡DocShield](https://docshield.tungstenautomation.com/AtalasoftDotImage/en_US/11.5.0-8wax4k031j/print/AtalasoftDotImageDevelopersGuide_EN.pdf?utm_source=chatgpt.com)
        {
            engine.Translate(images, "text/plain", plaintext, textTranslator);
        }
        swTxt.Stop();
        Log(log, $"OCR->Text: {swTxt.Elapsed.TotalMilliseconds:n0} ms");

        // 2c) Optional layout JSON (only if JsonTranslator assembly is present)
        if (EMIT_LAYOUT_JSON) {
            var swJson = Stopwatch.StartNew();
            TryJsonLayout(engine, images, layoutJson);
            swJson.Stop();
            Log(log, $"OCR->JSON: {swJson.Elapsed.TotalMilliseconds:n0} ms (optional)");
        }

        overall.Stop();
        Log(log, $"Overall   : {overall.Elapsed.TotalMilliseconds:n0} ms");
        Log(log, "");

        Log(log, "Per-page timings (PDF translation):");
        foreach (var p in perPage) Log(log, $"  Page {p.page:000}: {p.ms:n0} ms");

        Console.WriteLine($"OK: {name}");
    }

    private static void Log(StreamWriter log, string message) => log.WriteLine(message);

    private static void EnsurePdfDecoderWithDpi(int dpi)
    {
        // Register or update PdfDecoder’s Resolution so PDF rasterizes at OCR-friendly DPI.  [oai_citation:6‡Atalasoft](https://www.atalasoft.com/kb2/KB/50067/HOWTO-Safely-Change-Set-Resolution-of-PdfDecoder?utm_source=chatgpt.com)
        for (int i = 0; i < RegisteredDecoders.Decoders.Count; i++)
        {
            if (RegisteredDecoders.Decoders[i] is PdfDecoder existing) { existing.Resolution = dpi; return; }
        }
        RegisteredDecoders.Decoders.Add(new PdfDecoder { Resolution = dpi });
    }

    private static int BuildPreprocessedTiffFromPdf(string pdfPath, string outTiff)
    {
        int pageCount = 0;
        using var src = new FileSystemImageSource(new[] { pdfPath }, true); // multi-page input.  [oai_citation:7‡Atalasoft](https://www.atalasoft.com/kb2/Print50279.aspx?utm_source=chatgpt.com)
        using var tiffEncoder = new TiffEncoder {
            Compression = TiffCompression.Lzw,
            SaveMethod  = TiffSaveMethod.MultiPage
        };

        AtalaImage? first = null;
        try
        {
            for (int i = 0; i < src.TotalImages; i++)
            {
                using var page = src[i];
                using var cleaned = ApplyCleanup(page); // Doc cleanup is standard pre-OCR: deskew/binarize/despeckle.  [oai_citation:8‡NuGet](https://www.nuget.org/packages/Atalasoft.dotImage.AdvancedDocClean.x64/11.0.0.8478?utm_source=chatgpt.com)

                if (pageCount == 0)
                {
                    first = cleaned.Clone();
                    using var fs = File.Create(outTiff);
                    tiffEncoder.Save(first, fs);
                }
                else
                {
                    using var fs = new FileStream(outTiff, FileMode.Open, FileAccess.ReadWrite, FileShare.None);
                    fs.Seek(0, SeekOrigin.End);
                    tiffEncoder.Append(cleaned, fs);
                }
                pageCount++;
            }
        }
        finally { first?.Dispose(); }

        return pageCount;
    }

    private static AtalaImage ApplyCleanup(AtalaImage source)
    {
        var img = source.Clone();

        using (var cmd = new AutoDeskewCommand()) { cmd.ApplyInPlace(img); }  // deskew
        using (var cmd = new BinarizeCommand { BinarizationMethod = BinarizeMethod.DynamicThreshold }) { cmd.ApplyInPlace(img); } // dynamic binarize.  [oai_citation:9‡DocShield](https://docshield.tungstenautomation.com/AtalasoftDotImage/en_US/11.5.0-8wax4k031j/help/DotImage/html/M_Atalasoft_Imaging_ImageProcessing_Document_BinarizeCommand_PerformActualCommand.htm?utm_source=chatgpt.com)
        using (var cmd = new DespeckleCommand()) { cmd.ApplyInPlace(img); }   // despeckle

        return img;
    }

    private static void TryJsonLayout(OcrEngine engine, ImageSource images, string outPath)
    {
        try
        {
            // Optional JSON layout translator (if WebControls OCR assembly present)
            var t = Type.GetType("Atalasoft.Imaging.WebControls.OCR.JsonTranslator, Atalasoft.dotImage.WebControls", throwOnError:false);
            if (t == null) return;
            using var translator = (ITranslator)Activator.CreateInstance(t)!;
            engine.Translate(images, "application/json", outPath, translator);
        }
        catch { /* optional */ }
    }
}