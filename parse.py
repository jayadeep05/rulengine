import os
import PyPDF2

def extract_pdfs():
    docs = [
        "Place Order V3 API _ Upstox Developer API.pdf",
        "Modify Order V3 API _ Upstox Developer API.pdf",
        "Cancel Order V3 API _ Upstox Developer API.pdf",
        "Exit all positions API _ Upstox Developer API.pdf",
        "Intraday Candle Data V3 API _ Upstox Developer API.pdf",
        "Full Market Quotes API _ Upstox Developer API.pdf"
    ]
    
    with open('parsed_docs.txt', 'w', encoding='utf-8') as out:
        for doc in docs:
            out.write(f"\n\n--- {doc} ---\n")
            try:
                reader = PyPDF2.PdfReader(f"API docs/{doc}")
                for page in reader.pages:
                    out.write(page.extract_text() + "\n")
            except Exception as e:
                out.write(f"Failed to read: {e}\n")

if __name__ == '__main__':
    extract_pdfs()
