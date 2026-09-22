import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Check, Copy } from "lucide-react";
import { toast } from "sonner";

function CodeBlock({ language, children }: { language?: string; children: string }) {
  const [copied, setCopied] = useState(false);
  const code = children.replace(/\n$/, "");

  const copy = async () => {
    await navigator.clipboard.writeText(code);
    setCopied(true);
    toast.success("Copied code");
    setTimeout(() => setCopied(false), 1400);
  };

  return (
    <div className="my-3 overflow-hidden rounded-md border border-border bg-elevated">
      <div className="flex items-center justify-between border-b border-border px-3 py-1.5">
        <span className="font-mono text-2xs uppercase tracking-wider text-subtle">
          {language || "text"}
        </span>
        <button
          type="button"
          onClick={copy}
          className="flex items-center gap-1 text-2xs text-muted hover:text-fg"
        >
          {copied ? <Check size={12} className="text-success" /> : <Copy size={12} />}
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <pre className="overflow-x-auto p-3 font-mono text-xs leading-relaxed text-fg">
        <code>{code}</code>
      </pre>
    </div>
  );
}

export function Markdown({ content }: { content: string }) {
  return (
    <div className="text-sm leading-relaxed text-fg">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          code({ className, children, ...props }) {
            const match = /language-(\w+)/.exec(className || "");
            const text = String(children);
            const inline = !match && !text.includes("\n");
            if (inline) {
              return (
                <code
                  className="rounded-xs bg-hover px-1.5 py-0.5 font-mono text-xs text-accent"
                  {...props}
                >
                  {children}
                </code>
              );
            }
            return <CodeBlock language={match?.[1]}>{text}</CodeBlock>;
          },
          a({ children, ...props }) {
            return (
              <a
                className="text-accent underline decoration-accent/30 underline-offset-2 hover:decoration-accent"
                {...props}
              >
                {children}
              </a>
            );
          },
          blockquote({ children }) {
            return (
              <blockquote className="my-3 border-l-2 border-accent/40 pl-3 text-muted">
                {children}
              </blockquote>
            );
          },
          table({ children }) {
            return (
              <div className="my-3 overflow-x-auto rounded-md border border-border">
                <table className="w-full min-w-80 border-collapse text-sm">{children}</table>
              </div>
            );
          },
          th({ children }) {
            return (
              <th className="border-b border-border bg-elevated px-3 py-2 text-left text-xs font-medium text-muted">
                {children}
              </th>
            );
          },
          td({ children }) {
            return <td className="border-b border-border px-3 py-2 text-sm">{children}</td>;
          },
          h1({ children }) {
            return <h1 className="mt-4 mb-2 font-display text-xl tracking-tight">{children}</h1>;
          },
          h2({ children }) {
            return <h2 className="mt-4 mb-2 font-display text-lg tracking-tight">{children}</h2>;
          },
          h3({ children }) {
            return <h3 className="mt-3 mb-1 text-sm font-medium">{children}</h3>;
          },
          ul({ children }) {
            return <ul className="my-2 ml-5 list-disc space-y-1">{children}</ul>;
          },
          ol({ children }) {
            return <ol className="my-2 ml-5 list-decimal space-y-1">{children}</ol>;
          },
          li({ children, className }) {
            return (
              <li className={className?.includes("task-list-item") ? "ml-[-1.25rem] list-none" : ""}>
                {children}
              </li>
            );
          },
          p({ children }) {
            return <p className="mb-2 last:mb-0">{children}</p>;
          },
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
