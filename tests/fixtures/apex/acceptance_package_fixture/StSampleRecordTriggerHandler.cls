public class StSampleRecordTriggerHandler {
    private void generateSampleRecordLinks() {}
    public void andFinally() {
        if (Trigger.isInsert) generateSampleRecordLinks();
    }
}
