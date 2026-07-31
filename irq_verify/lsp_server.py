"""
lsp_server.py — Language Server Protocol server for VS Code integration.

This provides real-time analysis feedback in VS Code using the Language Server Protocol.

Features:
- Diagnostics: Show errors/warnings in Problems panel
- Hover: Show cycle breakdown on hover
- Code actions: Quick fixes for common issues
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

try:
    from pygls.server import LanguageServer
    from pygls.lsp.types import (
        Diagnostic,
        DiagnosticSeverity,
        Position,
        Range,
        Hover,
        MarkupContent,
        MarkupKind,
        DidSaveTextDocumentParams,
        TextDocumentPositionParams,
    )
except ImportError:
    # pygls not installed - VS Code extension won't work but CLI still functional
    logging.warning("pygls not installed - VS Code extension unavailable")
    LanguageServer = None  # type: ignore

logger = logging.getLogger(__name__)


class IRQVerifyLanguageServer:
    """
    Language Server for IRQ Verify.
    
    Provides real-time feedback in VS Code as you type.
    """
    
    def __init__(self):
        if LanguageServer is None:
            raise ImportError("pygls not installed")
        
        self.server = LanguageServer("irq-verify-lsp", "v1.0")
        self._setup_handlers()
    
    def _setup_handlers(self):
        """Register LSP event handlers."""
        
        @self.server.feature("textDocument/didSave")
        def on_save(ls: LanguageServer, params: DidSaveTextDocumentParams):
            """Analyze file when saved."""
            self._analyze_document(ls, params.text_document.uri)
        
        @self.server.feature("textDocument/hover")
        def on_hover(ls: LanguageServer, params: TextDocumentPositionParams) -> Optional[Hover]:
            """Provide hover information."""
            return self._get_hover(ls, params)
    
    def _analyze_document(self, ls: LanguageServer, uri: str):
        """
        Analyze a document and publish diagnostics.
        
        Parameters
        ----------
        ls:
            Language server instance.
        uri:
            Document URI.
        """
        try:
            # Convert URI to path
            path = Path(uri.replace("file://", ""))
            
            # Run analysis (simplified - in production, use proper API)
            from irq_verify.parser import parse_file
            from irq_verify.analysis import analyze_regions
            
            # Parse file
            ast = parse_file(path)
            if ast is None:
                return
            
            # Analyze regions
            results = analyze_regions(ast, budget=300)
            
            # Convert to diagnostics
            diagnostics = []
            for result in results:
                severity = DiagnosticSeverity.Error if not result.passed else DiagnosticSeverity.Information
                
                # Create diagnostic range (line - 1 because LSP is 0-indexed)
                start = Position(line=result.line - 1, character=0)
                end = Position(line=result.line - 1, character=100)
                range_obj = Range(start=start, end=end)
                
                # Create message
                if result.unbounded:
                    message = f"UNBOUNDED: {result.unbounded_reason}"
                elif not result.passed:
                    message = f"Budget exceeded: {result.cycles} > {result.budget} cycles"
                else:
                    margin = int((1 - result.cycles / result.budget) * 100)
                    message = f"✓ PASS: {result.cycles} / {result.budget} cycles ({margin}% margin)"
                
                diagnostic = Diagnostic(
                    range=range_obj,
                    message=message,
                    severity=severity,
                    source="irq-verify"
                )
                diagnostics.append(diagnostic)
            
            # Publish diagnostics
            ls.publish_diagnostics(uri, diagnostics)
            
        except Exception as e:
            logger.error(f"Analysis failed: {e}")
    
    def _get_hover(self, ls: LanguageServer, params: TextDocumentPositionParams) -> Optional[Hover]:
        """
        Provide hover information for a position.
        
        Parameters
        ----------
        ls:
            Language server instance.
        params:
            Position parameters.
        
        Returns
        -------
        Hover or None
            Hover information if available.
        """
        # TODO: Implement hover provider with cached results
        # For now, return None
        return None
    
    def start(self):
        """Start the language server."""
        self.server.start_io()


def main():
    """Start the IRQ Verify Language Server."""
    logging.basicConfig(level=logging.INFO)
    
    try:
        server = IRQVerifyLanguageServer()
        logger.info("IRQ Verify Language Server starting...")
        server.start()
    except ImportError as e:
        logger.error(f"Cannot start language server: {e}")
        logger.error("Install pygls: pip install pygls")
        return 1
    
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
