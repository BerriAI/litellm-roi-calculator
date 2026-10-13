import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import type { Settings } from "./api";
import { AzureDevOpsConnection, azureDevOpsConnectionFields } from "./AzureDevOpsConnection";
import { Field, type FormValues } from "./configuration";
import { GitHubConnection, githubConnectionFields } from "./GitHubConnection";

export function GitConnection({ values, saved, locked, update, save, onBusy }: {
  values: FormValues; saved: Settings; locked: (name: keyof FormValues) => boolean;
  update: (name: keyof FormValues, value: string) => void;
  save: (fields: string[]) => Promise<Settings>; onBusy: (busy: boolean) => void;
}) {
  const providers = [{ value: "github", label: "GitHub" }, { value: "azure_devops", label: "Azure DevOps" }];
  return <div className="form-fields">
    <Field label="Repository host" locked={locked("git_provider")}>
      <Select items={providers} value={values.git_provider} disabled={locked("git_provider")}
        onValueChange={value => { if (value !== null) update("git_provider", value); }}>
        <SelectTrigger className="w-full" aria-label="Repository host"><SelectValue /></SelectTrigger>
        <SelectContent className="p-1">{providers.map(item => <SelectItem value={item.value} key={item.value}>{item.label}</SelectItem>)}</SelectContent>
      </Select>
    </Field>
    {values.git_provider === "azure_devops"
      ? <AzureDevOpsConnection values={values} saved={saved} locked={locked} update={update}
          saveConnection={() => save(azureDevOpsConnectionFields)} onBusy={onBusy} />
      : <GitHubConnection values={values} saved={saved} locked={locked} update={update}
          saveConnection={() => save(githubConnectionFields)} onBusy={onBusy} />}
  </div>;
}
