import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneDark } from "react-syntax-highlighter/dist/esm/styles/prism";
import { Check, Copy } from "lucide-react";

function CodeBlock({ language, children }) {
  const [copied, setCopied] = useState(false);
  const code = String(children).replace(/\n$/, "");

  const copy = () => {
    navigator.clipboard.writeText(code);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  return (
    <div className="my-3 overflow-hidden rounded-md border border-border bg-surface-2">
      <div className="flex items-center justify-between border-b border-border px-3 py-1.5">
        <span className="font-mono text-xs text-text-tertiary">{language || "text"}</span>
        <button
          onClick={copy}
          className="flex items-center gap-1 text-xs text-text-tertiary hover:text-text-secondary"
        >
          {copied ? <Check size={12} className="text-success" /> : <Copy size={12} />}
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <SyntaxHighlighter
        language={language}
        style={oneDark}
        customStyle={{ margin: 0, background: "transparent", padding: "0.75rem", fontSize: "0.8125rem" }}
      >
        {code}
      </SyntaxHighlighter>
    </div>
  );
}

export default function Markdown({ content }) {
  return (
    <div className="prose-astra">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          code({ inline, className, children, ...props }) {
            const match = /language-(\w+)/.exec(className || "");
            if (inline) {
              return (
                <code className="rounded bg-surface-3 px-1.5 py-0.5 font-mono text-[0.85em] text-accent" {...props}>
                  {children}
                </code>
              );
            }
            return <CodeBlock language={match?.[1]}>{children}</CodeBlock>;
          },
          a({ children, ...props }) {
            return (
              <a className="text-accent underline decoration-accent/30 underline-offset-2 hover:text-accent-hover" {...props}>
                {children}
              </a>
            );
          },
          blockquote({ children }) {
            return <blockquote className="border-l-2 border-accent/40 pl-3 text-text-secondary">{children}</blockquote>;
          },
          table({ children }) {
            return (
              <div className="my-3 overflow-x-auto rounded-md border border-border">
                <table className="w-full border-collapse text-sm">{children}</table>
              </div>
            );
          },
          th({ children }) {
            return <th className="border-b border-border bg-surface-2 px-3 py-1.5 text-left font-medium">{children}</th>;
          },
          td({ children }) {
            return <td className="border-b border-border/60 px-3 py-1.5">{children}</td>;
          },
          h1({ children }) {
            return <h1 className="mb-2 mt-4 text-lg font-semibold text-text-primary">{children}</h1>;
          },
          h2({ children }) {
            return <h2 className="mb-2 mt-4 text-base font-semibold text-text-primary">{children}</h2>;
          },
          h3({ children }) {
            return <h3 className="mb-1 mt-3 text-sm font-semibold text-text-primary">{children}</h3>;
          },
          ul({ children }) {
            return <ul className="my-2 ml-5 list-disc space-y-1">{children}</ul>;
          },
          ol({ children }) {
            return <ol className="my-2 ml-5 list-decimal space-y-1">{children}</ol>;
          },
          li({ children, className }) {
            // GFM task-list items get a "task-list-item" className from remark-gfm
            return <li className={className?.includes("task-list-item") ? "ml-[-1.25rem] list-none" : ""}>{children}</li>;
          },
          p({ children }) {
            return <p className="mb-2 leading-relaxed last:mb-0">{children}</p>;
          },
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
