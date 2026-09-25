import { useRef, type ComponentProps } from "react";
import { Combobox, ComboboxInput, ComboboxContent, ComboboxList, ComboboxItem, ComboboxEmpty } from "@/components/ui/combobox";

// Keep free text valid: gateways can expose model aliases that aren't in /models.
export function TextPicker({ items, value, onChange, ...props }: {
  items: string[]; value: string; onChange: (value: string) => void;
} & Pick<ComponentProps<typeof ComboboxInput>, "name" | "type" | "required" | "disabled" | "placeholder">) {
  const anchor = useRef<HTMLDivElement>(null);
  return <div ref={anchor} className="min-w-0">
    <Combobox items={[...new Set(items)]} inputValue={value} value={items.includes(value) ? value : null}
      onInputValueChange={(text, details) => { if (details.reason === "input-change" || details.reason === "clear-press") onChange(text); }}
      onValueChange={text => { if (text !== null) onChange(text); }} disabled={props.disabled}>
      <ComboboxInput {...props} className="w-full" />
      <ComboboxContent anchor={anchor}>
        <ComboboxEmpty>Enter a value to use it.</ComboboxEmpty>
        <ComboboxList>{(item: string) => <ComboboxItem key={item} value={item} className="break-all">{item}</ComboboxItem>}</ComboboxList>
      </ComboboxContent>
    </Combobox>
  </div>;
}
