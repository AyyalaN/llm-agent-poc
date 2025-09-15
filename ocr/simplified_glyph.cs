// Program.cs
// .NET console app built as a DLL (UseAppHost=false)
// Engine: GlyphReader (no TIFF preprocessing; direct PDF -> OCR).
// No 'using' statements—resources are disposed via try/finally (DisposeQuietly).
//
// Drop the built DLL into the same folder as your Atalasoft DLLs + license files.
// Ensure your GlyphReader resources folder is correct (see GLYPH_RESOURCES below).
// Run with: dotnet OcrBatchDemo.dll

using System;
using System.IO;
using System.Linq;
using System.Diagnostics;
using System.Globalization;

using Atalasoft.Imaging;
using Atalasoft.Imaging.Codec;
using Atalasoft.Imaging.Codec.Pdf;
using Atalasoft.Ocr;
using Atalasoft.Ocr.GlyphReader;

internal static class Program
{
    // ***** EDIT THESE *****
    private const string INPUT_DIR        = @"D:\ocr\in";
    private const string OUTPUT_DIR       = @"D:\ocr\out";
    // Point to the folder that CONTAINS OcrResources\GlyphReader\[vX] (see docs)
    // e.g. D:\ocr\gr where D:\ocr\gr\OcrResources\GlyphReader\5.0\... exists.
    private const string GLYPH_RESOURCES  = @"D:\ocr\gr";
    private const string OCR_LANGUAGE     = "en-US";
    private const int    PDF_RASTER_DPI   = 300;
    private const bool   EMIT_LAYOUT_JSON = true;   // requires WebControls OCR translator present

    private static int Main(string[] args)
    {
        if (!Directory.Exists(INPUT_DIR))  { Console.Error.WriteLine($"Input missing: {INPUT_DIR}"); return 2; }
        Directory.CreateDirectory(OUTPUT_DIR);
        if (!Directory.Exists(GLYPH_RESOURCES)) { Console.Error.WriteLine($"GlyphReader resources base missing: {GLYPH_RESOURCES}"); return 3; }

        // Ensure PDFs rasterize at a known DPI for OCR.
        EnsurePdfDecoderWithDpi(PDF_RASTER_DPI);

        var pdfs = Directory.EnumerateFiles(INPUT_DIR, "*.pdf", SearchOption.TopDirectoryOnly)
                            .OrderBy(p => p, StringComparer.OrdinalIgnoreCase)
                            .ToList();
        if (pdfs.Count == 0) { Console.WriteLine("No PDFs found."); return 0; }

        Console.WriteLine($"Input : {INPUT_DIR}");
        Console.WriteLine($"Output: {OUTPUT_DIR}");
        Console.WriteLine($"Glyph : {GLYPH_RESOURCES}  (expects OcrResources\\GlyphReader\\... under this)");
        Console.WriteLine($"Lang  : {OCR_LANGUAGE}");
        Console.WriteLine($"DPI   : {PDF_RASTER_DPI}");
        Console.WriteLine();

        foreach (var pdf in pdfs)
        {
            try { RunJobFor(pdf); }
            catch (Exception ex)
            {
                Console.Error.WriteLine($"FAIL: {Path.GetFileName(pdf)} -> {ex.GetType().Name}: {ex.Message}");
            }
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
        StreamWriter log = null;

        try
        {
            log = new StreamWriter(logPath, append: false);
            Log(log, $"== OCR Job: {name} ==");
            Log(log, $"Started   : {DateTimeOffset.Now:O}");
            Log(log, $"Input PDF : {pdfPath}");
            Log(log, $"Output Dir: {outDir}");
            Log(log, "");

            var overall = Stopwatch.StartNew();

            string searchablePdf = Path.Combine(outDir, $"{name}.searchable.pdf");
            string plaintext     = Path.Combine(outDir, $"{name}.txt");
            string layoutJson    = Path.Combine(outDir, $"{name}.layout.json");

            // OCR pieces
            OcrEngine engine = null;               // GlyphReaderEngine implements OcrEngine
            ImageSource images = null;             // PDF multipage source
            PdfTranslator pdfTranslator = null;
            TextTranslator textTranslator = null;

            try
            {
                // 1) Point GlyphReader to its resources and create engine
                // Loader must be created BEFORE engine per docs; it auto-registers the path.
                // Expectation: {GLYPH_RESOURCES}\OcrResources\GlyphReader\<version>\...
                GlyphReaderLoader loader = null;
                try
                {
                    loader = new GlyphReaderLoader(GLYPH_RESOURCES); // sets OcrResources/GlyphReader path(s)  [oai_citation:3‡atalasoft.com](https://www.atalasoft.com/kb2/Print50303.aspx?utm_source=chatgpt.com)
                }
                finally
                {
                    DisposeQuietly(loader); // harmless if not IDisposable in your build
                }

                var gr = new GlyphReaderEngine();   // Atalasoft.Ocr.GlyphReader.GlyphReaderEngine  [oai_citation:4‡DocShield](https://docshield.tungstenautomation.com/atalasoftdotimage/en_us/11.4.0-n632p3l96b/help/dotimage/html/T_Atalasoft_Ocr_GlyphReader_GlyphReaderEngine.htm?utm_source=chatgpt.com)
                engine = gr;
                gr.RecognitionCulture = new CultureInfo(OCR_LANGUAGE);
                gr.Initialize();                    // required prior to recognition  [oai_citation:5‡DocShield](https://docshield.tungstenautomation.com/atalasoftdotimage/en_us/11.4.0-n632p3l96b/help/dotimage/html/M_Atalasoft_Ocr_GlyphReader_GlyphReaderEngine_Initialize.htm?utm_source=chatgpt.com)

                // 2) Use the PDF directly as an ImageSource
                images = new FileSystemImageSource(new[] { pdfPath }, true);

                // 3) Searchable PDF
                var swPdf = Stopwatch.StartNew();
                pdfTranslator = new PdfTranslator();  // creates image-under-text PDFs from OCR output  [oai_citation:6‡DocShield](https://docshield.tungstenautomation.com/AtalasoftDotImage/en_US/11.5.0-8wax4k031j/help/DotImage/html/T_Atalasoft_Ocr_PdfTranslator.htm?utm_source=chatgpt.com)
                engine.Translate(images, "application/pdf", searchablePdf, pdfTranslator);
                swPdf.Stop();
                Log(log, $"OCR->PDF : {swPdf.Elapsed.TotalMilliseconds:n0} ms");

                // 4) Plain text
                var swTxt = Stopwatch.StartNew();
                textTranslator = new TextTranslator();
                engine.Translate(images, "text/plain", plaintext, textTranslator);
                swTxt.Stop();
                Log(log, $"OCR->Text: {swTxt.Elapsed.TotalMilliseconds:n0} ms");

                // 5) Optional layout JSON
                if (EMIT_LAYOUT_JSON)
                {
                    var swJson = Stopwatch.StartNew();
                    TryJsonLayout(engine, images, layoutJson);
                    swJson.Stop();
                    Log(log, $"OCR->JSON: {swJson.Elapsed.TotalMilliseconds:n0} ms (optional)");
                }
            }
            finally
            {
                DisposeQuietly(textTranslator);
                DisposeQuietly(pdfTranslator);
                DisposeQuietly(images);   // safe if not IDisposable
                DisposeQuietly(engine);
            }

            overall.Stop();
            Log(log, $"Overall   : {overall.Elapsed.TotalMilliseconds:n0} ms");

            Console.WriteLine($"OK: {name}");
        }
        finally
        {
            DisposeQuietly(log);
        }
    }

    private static void EnsurePdfDecoderWithDpi(int dpi)
    {
        for (int i = 0; i < RegisteredDecoders.Decoders.Count; i++)
        {
            var existing = RegisteredDecoders.Decoders[i] as PdfDecoder;
            if (existing != null) { existing.Resolution = dpi; return; }
        }
        RegisteredDecoders.Decoders.Add(new PdfDecoder { Resolution = dpi });
    }

    private static void TryJsonLayout(OcrEngine engine, ImageSource images, string outPath)
    {
        try
        {
            // Optional JSON layout translator (WebControls OCR)
            var t = Type.GetType("Atalasoft.Imaging.WebControls.OCR.JsonTranslator, Atalasoft.dotImage.WebControls", false);
            if (t == null) return;

            ITranslator translator = null;
            try
            {
                translator = (ITranslator)Activator.CreateInstance(t);
                engine.Translate(images, "application/json", outPath, translator);
            }
            finally
            {
                DisposeQuietly(translator);
            }
        }
        catch
        {
            // optional; ignore if unavailable
        }
    }

    private static void Log(StreamWriter log, string message)
    {
        if (log != null) log.WriteLine(message);
    }

    private static void DisposeQuietly(object obj)
    {
        try { (obj as IDisposable)?.Dispose(); } catch { }
    }
}