#!/usr/bin/env python
import logging

from photoshop import Session

logger = logging.getLogger(__name__)

PRIMARY_TEXT_LAYER_NAME = "Temp_Text_Layer"

def adjust_text_layer_size(ps, layer_name, percentage):
    """
    Adjusts the text size AND leading of the specified text layer based on the given percentage.

    Steps:
      1) Retrieve the current text size (in points).
      2) Calculate new_size = current_size * (percentage / 100).
      3) Set text_layer.TextItem.size = new_size
      4) Disable auto-leading if it's on, then set text_layer.TextItem.leading = new_size
    """
    doc = ps.app.activeDocument
    try:
        text_layer = doc.ArtLayers[layer_name]
    except Exception as e:
        ps.echo(f"Text layer '{layer_name}' not found: {e}")
        return

    try:
        default_size = float(text_layer.TextItem.size)
        ps.echo(f"Default text size for layer '{layer_name}' is: {default_size} points.")

        if percentage == 100:
            ps.echo(f"Percentage is 100; no adjustment applied to layer '{layer_name}'.")
            return

        new_size = default_size * (percentage / 100.0)
        text_layer.TextItem.size = new_size

        # Disable auto-leading if needed, then set the same numeric value
        if text_layer.TextItem.useAutoLeading:
            text_layer.TextItem.useAutoLeading = False

        text_layer.TextItem.leading = new_size

        ps.echo(f"Adjusted text size for layer '{layer_name}' to {new_size} pts (leading={new_size}).")
    except Exception as e:
        ps.echo(f"Error adjusting text size/leading for layer '{layer_name}': {e}")


def adjust_text_sizes(ps, percentage):
    """
    Only adjusts the size/leading of Temp_Text_Layer.
    (CTA and Button layers are ignored.)
    """
    adjust_text_layer_size(ps, PRIMARY_TEXT_LAYER_NAME, percentage)


def prevent_text_overflow(ps, layer_name):
    """
    Simple overflow prevention: checks text length and auto-shrinks if too long.
    This prevents text from overflowing the text box.
    """
    doc = ps.app.activeDocument
    try:
        text_layer = doc.ArtLayers[layer_name]
        text_content = text_layer.TextItem.contents
        text_length = len(text_content)
        
        # If text is very long, reduce size to prevent overflow
        reduction_needed = False
        shrink_factor = 1.0
        
        if text_length > 150:
            reduction_needed = True
            shrink_factor = 0.70  # 70% of current size
            reason = f"{text_length} chars (very long)"
        elif text_length > 120:
            reduction_needed = True
            shrink_factor = 0.80  # 80%
            reason = f"{text_length} chars (long)"
        elif text_length > 100:
            reduction_needed = True
            shrink_factor = 0.90  # 90%
            reason = f"{text_length} chars (above average)"
        
        if reduction_needed:
            current_size = float(text_layer.TextItem.size)
            new_size = current_size * shrink_factor
            text_layer.TextItem.size = new_size
            
            if text_layer.TextItem.useAutoLeading:
                text_layer.TextItem.useAutoLeading = False
            text_layer.TextItem.leading = new_size
            
            percentage = int(shrink_factor * 100)
            logger.info("Auto-reduced text size to %d%% - text is %s", percentage, reason)

    except Exception as e:
        logger.warning("Overflow protection error for '%s': %s", layer_name, e)


def main():
    desired_percentage = 150  # example for local testing

    with Session() as ps:
        if len(ps.app.documents) < 1:
            ps.echo("No active document found. Creating a new document for testing.")
            doc = ps.app.documents.add(
                320, 240, 72, None, ps.NewDocumentMode.NewRGB, ps.DocumentFill.White
            )
        adjust_text_sizes(ps, desired_percentage)


if __name__ == "__main__":
    main()
