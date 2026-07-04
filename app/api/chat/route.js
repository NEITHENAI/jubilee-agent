import { NextResponse } from 'next/server';
import fs from 'fs';
import path from 'path';
import kbData from '../../../jubilee_products_kb.json';
import pdData from '../../../jubilee_provider_directory.json';

export async function POST(request) {
  try {
    const { messages } = await request.json();
    
    const lastUserMessage = messages[messages.length - 1]?.content || "";

    // 1. Read System Prompt
    // Vercel handles process.cwd() reliably when path.join is used directly like this
    const promptPath = path.join(process.cwd(), 'jubilee_agent_system_prompt.md');
    let systemPrompt = "You are a helpful Jubilee AI assistant.";
    if (fs.existsSync(promptPath)) {
      systemPrompt = fs.readFileSync(promptPath, 'utf8');
    }

    // 2. Perform Retrieval from JSON imports
    let contextStr = "Additional context from knowledge base:\n\n";
    let found = false;
    const query = lastUserMessage.toLowerCase();
    const searchTerms = query.split(' ').filter(w => w.length > 3);

    if (Array.isArray(kbData)) {
      for (const item of kbData) {
          const textToSearch = ((item.text || "") + " " + (item.section_label || "")).toLowerCase();
          if (textToSearch.includes(query) || searchTerms.some(w => textToSearch.includes(w))) {
              contextStr += `[Product Info]: ${item.text || item.section_label}\n`;
              found = true;
          }
      }
    }
    
    if (Array.isArray(pdData)) {
      for (const item of pdData) {
          const textToSearch = JSON.stringify(item).toLowerCase();
          if (textToSearch.includes(query) || searchTerms.some(w => textToSearch.includes(w))) {
              contextStr += `[Provider Info]: ${JSON.stringify(item)}\n`;
              found = true;
          }
      }
    }

    if (found) contextStr = contextStr.substring(0, 4000);

    // 3. Construct Final Prompt Array for DeepSeek
    const apiMessages = [
      { role: "system", content: systemPrompt }
    ];

    for (let i = 0; i < messages.length - 1; i++) {
        if (i === 0 && messages[i].role === 'assistant' && messages[i].content.includes('Hello! I am the')) continue;
        apiMessages.push(messages[i]);
    }

    const augmentedPrompt = found ? `${contextStr}\n\nUser Question: ${lastUserMessage}` : lastUserMessage;
    apiMessages.push({ role: "user", content: augmentedPrompt });

    // 4. Call DeepSeek API securely using the environment variable
    const apiKey = process.env.DEEPSEEK_API_KEY;
    
    if (!apiKey || apiKey === 'your_api_key_here') {
      return NextResponse.json({ reply: "Configuration error: DEEPSEEK_API_KEY is missing from Vercel Environment Variables." }, { status: 500 });
    }

    const response = await fetch("https://api.deepseek.com/chat/completions", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${apiKey}`
      },
      body: JSON.stringify({
        model: "deepseek-chat",
        messages: apiMessages,
        temperature: 0.7,
        max_tokens: 1024
      })
    });

    if (!response.ok) {
        throw new Error(`DeepSeek API error: ${response.status}`);
    }

    const data = await response.json();
    return NextResponse.json({ reply: data.choices[0].message.content });

  } catch (error) {
    console.error("API Route Error:", error);
    return NextResponse.json({ reply: `Server Error: ${error.message}` }, { status: 500 });
  }
}
