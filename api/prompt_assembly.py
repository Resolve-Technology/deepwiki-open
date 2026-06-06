"""Prompt envelope for server-side generation, copied from the websocket chat path.

Today's generation flow double-wraps every LLM call: the frontend sends the
page/structure prompt as the websocket message, and ``websocket_wiki.py`` wraps
it in its own code-analyst system prompt and envelope (``/no_think`` prefix,
optional file content / RAG context blocks, ``<query>...`` tail). The server-side
engine must reproduce that envelope byte-for-byte, so the assembly code is
extracted here verbatim. Do NOT "clean up" the formatting — any change alters
what the model sees versus the browser-driven flow.

The websocket handler is intentionally left untouched; this is a copy, not a
refactor (the duplication is the price of not destabilising the chat path).
"""
import logging

from api.config import configs
from api.data_pipeline import count_tokens
from api.prompt_fit import fit_to_budget, prompt_token_budget

logger = logging.getLogger(__name__)


def select_generation_system_prompt(repo_type: str, repo_url: str,
                                    repo_name: str, language: str) -> str:
    """The websocket's non-deep-research system prompt (verbatim)."""
    language_code = language or configs["lang_config"]["default"]
    supported_langs = configs["lang_config"]["supported_languages"]
    language_name = supported_langs.get(language_code, "English")

    return f"""<role>
You are an expert code analyst examining the {repo_type} repository: {repo_url} ({repo_name}).
You provide direct, concise, and accurate information about code repositories.
You NEVER start responses with markdown headers or code fences.
IMPORTANT:You MUST respond in {language_name} language.
</role>

<guidelines>
- Answer the user's question directly without ANY preamble or filler phrases
- DO NOT include any rationale, explanation, or extra comments.
- Strictly base answers ONLY on existing code or documents
- DO NOT speculate or invent citations.
- DO NOT start with preambles like "Okay, here's a breakdown" or "Here's an explanation"
- DO NOT start with markdown headers like "## Analysis of..." or any file path references
- DO NOT start with ```markdown code fences
- DO NOT end your response with ``` closing fences
- DO NOT start by repeating or acknowledging the question
- JUST START with the direct answer to the question

<example_of_what_not_to_do>
```markdown
## Analysis of `adalflow/adalflow/datasets/gsm8k.py`

This file contains...
```
</example_of_what_not_to_do>

- Format your response with proper markdown including headings, lists, and code blocks WITHIN your answer
- For code analysis, organize your response with clear sections
- Think step by step and structure your answer logically
- Start with the most relevant information that directly addresses the user's query
- Be precise and technical when discussing code
- Your response language should be in the same language as the user's query
</guidelines>

<style>
- Use concise, direct language
- Prioritize accuracy over verbosity
- When showing code, include line numbers and file paths when relevant
- Use markdown formatting to improve readability
</style>"""


def format_context_text(retrieved_documents) -> str:
    """Group retrieved documents by file path, exactly as the websocket does.

    ``retrieved_documents`` is the raw return of ``RAG.__call__``; returns ""
    when nothing was retrieved (the envelope then carries the no-RAG note).
    """
    if not (retrieved_documents and retrieved_documents[0].documents):
        return ""

    documents = retrieved_documents[0].documents
    logger.info(f"Retrieved {len(documents)} documents")

    # Group documents by file path
    docs_by_file = {}
    for doc in documents:
        file_path = doc.meta_data.get('file_path', 'unknown')
        if file_path not in docs_by_file:
            docs_by_file[file_path] = []
        docs_by_file[file_path].append(doc)

    # Format context text with file path grouping
    context_parts = []
    for file_path, docs in docs_by_file.items():
        # Add file header with metadata
        header = f"## File Path: {file_path}\n\n"
        # Add document content
        content = "\n\n".join([doc.text for doc in docs])

        context_parts.append(f"{header}{content}")

    # Join all parts with clear separation
    return "\n\n" + "-" * 10 + "\n\n".join(context_parts)


def assemble_envelope(system_prompt: str, query: str, *,
                      conversation_history: str = "",
                      file_content: str = "",
                      file_path: str = "",
                      context_text: str = "",
                      provider: str = "") -> str:
    """The exact prompt assembly from ``websocket_wiki.py`` (incl. budget fit).

    ``provider`` selects the token budget (claude gets the large one) just as
    the websocket passes ``request.provider`` to ``prompt_token_budget``.
    """
    # Fit oversized inputs (full program sources) to the provider's context budget
    file_content, context_text = fit_to_budget(
        file_content=file_content,
        context_text=context_text,
        base_tokens=count_tokens(
            system_prompt + conversation_history + query,
            is_ollama_embedder=(provider == "ollama"),
        ),
        budget=prompt_token_budget(provider),
    )

    # Create the prompt with context
    prompt = f"/no_think {system_prompt}\n\n"

    if conversation_history:
        prompt += f"<conversation_history>\n{conversation_history}</conversation_history>\n\n"

    if file_content:
        prompt += f"<currentFileContent path=\"{file_path}\">\n{file_content}\n</currentFileContent>\n\n"

    # Only include context if it's not empty
    CONTEXT_START = "<START_OF_CONTEXT>"
    CONTEXT_END = "<END_OF_CONTEXT>"
    if context_text.strip():
        prompt += f"{CONTEXT_START}\n{context_text}\n{CONTEXT_END}\n\n"
    else:
        # Add a note that we're skipping RAG due to size constraints or because it's the isolated API
        logger.info("No context available from RAG")
        prompt += "<note>Answering without retrieval augmentation.</note>\n\n"

    prompt += f"<query>\n{query}\n</query>\n\nAssistant: "

    return prompt
