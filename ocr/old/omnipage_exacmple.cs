// Program.cs
// .NET 8 Console – Single-file PoC for OCR'ing PDFs with Atalasoft DotImage + OmniPage
// Artifacts per PDF:
//   1) <name>.searchable.pdf (image-under-text PDF)
//   2) <name>.txt           (plain text)
//   3) <name>.layout.json   (optional, if JsonTranslator is available)

using System;
using System.Globalization;
using System.IO;
using System.Linq;
using Atalasoft.Imaging;
using Atalasoft.Imaging.Codec;
using Atalasoft.Imaging.Codec.Pdf;
using Atalasoft.Ocr;
using Atalasoft.Ocr.OmniPage;

class Program
{
    // Configure these or pass as args[0], args[1]
    private static string InputDir  = Environment.GetEnvironmentVariable("OCR_INPUT_DIR")  ?? @"C:\ocr\in";
    private static string OutputDir = Environment.GetEnvironmentVariable("OCR_OUTPUT_DIR") ?? @"C:\ocr\out";

    // IMPORTANT: Set path to OmniPage resources (unzipped “OmniPage Resources” folder)
    // You can set OMNIPAGE_RESOURCES env var or hardcode here:
    private static string OmniPageResources = Environment.GetEnvironmentVariable("OMNIPAGE_RESOURCES") ?? @"C:\Atalasoft\OmniPageResources";

    static int Main(string[] args)
    {
        if (args?.Length >= 1) InputDir  = args[0];
        if (args?.Length >= 2) OutputDir = args[1];

        if (!Directory.Exists(InputDir))
        {
            Console.Error.WriteLine($"Input directory not found: {InputDir}");
            return 1;
        }
        Directory.CreateDirectory(OutputDir);

        // 1) Register PDF decoding & set sensible rasterization DPI (200–300 is typical for OCR)
        SafelySetPdfDecoderResolution(300); // per Atalasoft KB guidance

        // 2) Load OmniPage engine resources & create engine
        if (!Directory.Exists(OmniPageResources))
        {
            Console.Error.WriteLine($"OmniPage resources not found at: {OmniPageResources}");
            Console.Error.WriteLine("Set OMNIPAGE_RESOURCES to the unpacked OmniPage Resources folder.");
            return 2;
        }

        using var loader = new OmniPageLoader(OmniPageResources);
        using var engine = new OmniPageEngine();

        // Basic engine configuration
        // - Recognition language(s)
        engine.RecognitionCultures = new[] { new CultureInfo("en-US") };
        // - Let engine handle autorotate/deskew internally (preprocessing)
        //   If your build exposes NativePreprocessingOptions, you can also set it here.
        //   Otherwise, the PdfTranslator’s AutoPageRotation plus OmniPage’s own routines are sufficient for a PoC.

        // 3) Process each PDF in the folder
        var pdfs = Directory.EnumerateFiles(InputDir, "*.pdf", SearchOption.TopDirectoryOnly).ToList();
        if (pdfs.Count == 0)
        {
            Console.WriteLine("No PDFs found.");
            return 0;
        }

        Console.WriteLine($"Found {pdfs.Count} PDFs. Starting OCR ...");

        foreach (var pdfPath in pdfs)
        {
            try
            {
                var name = Path.GetFileNameWithoutExtension(pdfPath);
                var destBase = Path.Combine(OutputDir, name);
                Directory.CreateDirectory(OutputDir);

                // Build an ImageSource over the single PDF (multi-frame)
                using var images = new FileSystemImageSource(new[] { pdfPath }, /* all frames */ true);

                // 3a) Searchable PDF (image-under-text)
                var searchablePdfPath = destBase + ".searchable.pdf";
                using (var pdfTranslator = new PdfTranslator())
                {
                    // Helpful option: rotate a page if all detected text regions share the same rotation
                    pdfTranslator.AutoPageRotation = true;
                    // Engine will select text-under-image form; PdfTranslator is the “searchable PDF” translator.
                    engine.Translate(images, "application/pdf", searchablePdfPath, pdfTranslator);
                }

                // 3b) Plain text
                var txtPath = destBase + ".txt";
                using (var textTranslator = new TextTranslator())
                {
                    engine.Translate(images, "text/plain", txtPath, textTranslator);
                }

                // 3c) Optional layout JSON (only if JsonTranslator is available at runtime)
                // JsonTranslator lives in Atalasoft.Imaging.WebControls.OCR.
                // We load it via reflection so this program still runs if that assembly isn't installed.
                var layoutJsonPath = destBase + ".layout.json";
                TryTranslateLayoutJson(engine, images, layoutJsonPath);

                Console.WriteLine($"OK: {name}");
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine($"FAIL on '{pdfPath}': {ex.Message}");
            }
        }

        Console.WriteLine("Done.");
        return 0;
    }

    private static void SafelySetPdfDecoderResolution(int newRes)
    {
        var pdfLock = new object();
        lock (pdfLock)
        {
            // Update existing PdfDecoder if already registered; else add one with desired DPI.
            for (int i = 0; i < RegisteredDecoders.Decoders.Count; i++)
            {
                if (RegisteredDecoders.Decoders[i] is PdfDecoder existing)
                {
                    existing.Resolution = newRes;
                    return;
                }
            }
            RegisteredDecoders.Decoders.Add(new PdfDecoder() { Resolution = newRes });
        }
    }

    private static void TryTranslateLayoutJson(OmniPageEngine engine, ImageSource images, string outputPath)
    {
        try
        {
            // Late-bind JsonTranslator to avoid hard dependency on WebControls assembly.
            var t = Type.GetType("Atalasoft.Imaging.WebControls.OCR.JsonTranslator, Atalasoft.dotImage.WebControls", throwOnError: false);
            if (t == null) return; // Not installed – skip quietly.

            using var translator = (ITranslator)Activator.CreateInstance(t)!;
            // Many builds accept "application/json"; if not, translator will still handle via Translate overload.
            engine.Translate(images, "application/json", outputPath, translator);
        }
        catch
        {
            // Non-fatal; layout JSON is optional for this PoC.
        }
    }
}